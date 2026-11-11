"""Detached runner for one Hermes lane wake: a turn, then more while addressed rows keep arriving.

The runner is the lane's stop guard. A Hermes turn cannot be blocked from ending
the way a Claude Stop hook blocks one, so the runner re-reads the lane's delivery
cursor after each turn and starts the next one itself, a bounded number of times.
"""
import argparse
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time

from . import hermes

MAX_ROUNDS = 3
FAILURE_RETRIES = 2  # delayed returns, one debounce apart, for rows posted during a failed turn
# A wake runs in the process of whoever posted the row. The lane's turn must not
# inherit that sender's credentials or agent identity, so it gets these and nothing else.
ENV_ALLOW = ('HOME', 'USER', 'LOGNAME', 'SHELL', 'PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ', 'TMPDIR',
             'XDG_STATE_HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME')
MAX_LINES = 20
MAX_BATCHES = 64  # of deliver.MAX_READ each, per scan call; a scan that stops short says so
MAX_RECORD = 1024 * 1024
KILL_GRACE = 10


def wants_turn(row, name, targets, resolved=None):
    """Every row that would have woken the lane, plus what delivery shows a busy session.

    While a turn runs, the wake path treats the lane as listening and starts nothing, and
    a wake with nothing unread starts no turn. The scan must therefore cover exactly the
    rows the wake path acts on: the lane by name or alias, a role it belongs to, or its
    ownership. `resolved` holds the row's recipients after alias resolution.
    """
    from .. import deliver
    if deliver.addressed_row(row, name, targets):
        return True
    if resolved is None:
        resolved = {row.get('to')}
    mine = bool(resolved & (targets - {'all'})) or row.get('owner') == name
    return (mine and row.get('frm') != name and row.get('kind') != 'fyi'
            and not row.get('digest_only')
            and not (row.get('kind') in ('reply', 'answer', 'ack') and row.get('re'))
            and all(isinstance(row.get(key), str) for key in ('id', 'frm', 'kind')))


def recipients(row, book):
    from ..bus import resolve
    out = set()
    for part in str(row.get('to', '')).split(','):
        try:
            out.add(resolve(part.strip(), book))
        except ValueError:
            out.add(part.strip())
    return out


def pending(manifest, name):
    """Rows for the lane after the shared delivery cursor, and the offset that consumes them.

    One read covers a bounded batch. A full batch with nothing for the lane says nothing
    about what follows it, so the scan moves the cursor past it and reads on. The last
    value is False when the scan stopped at its batch cap before the end of the log:
    "no lines" then means "not found yet", never "nothing unread".
    """
    from .. import deliver
    from ..bus import aliases
    targets = deliver.targets_for(manifest, name)
    book = aliases(manifest)
    complete = False
    for _ in range(MAX_BATCHES):
        identity, start, data = deliver.unread_batch(manifest, name)
        lines, offset = [], start
        for size, row in deliver.parsed_rows(data):
            if row is not None and wants_turn(row, name, targets, recipients(row, book)):
                if len(lines) >= MAX_LINES:
                    break
                lines.append(deliver.row_line(row, name))
            offset += size
        if lines or len(data) < deliver.MAX_READ or offset == start:
            # Reaching the end, or a single row larger than a batch, ends the scan for good.
            complete = bool(lines) or len(data) < deliver.MAX_READ or offset == start
            break
        advance(manifest, name, identity, offset)
    return identity, offset, lines, complete


def advance(manifest, name, identity, offset):
    """Rows handed over in the prompt are delivered. Never move a newer hook cursor backwards."""
    from .. import deliver
    from ..routing import locked
    from ..state import read_json, write_json
    cursor = deliver.cursor_path(manifest, name)
    with locked(cursor.with_suffix('.hook.lock')):
        prior = read_json(cursor)
        if isinstance(prior, dict) and prior.get('file') == identity and isinstance(prior.get('offset'), int):
            offset = max(offset, prior['offset'])
        write_json(cursor, {'file': identity, 'offset': offset})


def prompt(manifest, name, lines, round_number):
    inbox = f'coordkit --manifest {shlex.quote(str(manifest.file))} bus inbox {shlex.quote(name)} --full'
    head = f'Coordkit wake for lane {name}. Your bus name is {name}.'
    if round_number:
        head += ' More addressed work arrived while your last turn ran.'
    rows = ('\nAddressed rows since your last turn:\n' + '\n'.join(lines) + '\n') if lines else '\n'
    return (f'{head}\n{rows}'
            f'Read your whole bus inbox ({inbox}), handle pending addressed work, and reply on the bus.\n'
            'You run one turn at a time and cannot arm a background waiter: coordkit starts your next '
            'turn when addressed work arrives. Before you end this turn, write down anything a later '
            'turn needs (lane state file or profile memory), because no process of yours keeps running.\n')


def argv_for(binary, lane, text, usage_file, session):
    """One gateway-free turn. A one-shot run cannot create a titled session, so the first
    turn starts a new one and later turns resume the id it reported."""
    # The profile's hooks are the operator's own config; without consent Hermes skips them.
    argv = [binary, '-p', lane.profile, '--in', lane.cwd, '--no-restore-cwd', '--accept-hooks']
    if session:
        argv += ['--resume', session]
    argv += ['--output-format', 'json', '--usage-file', str(usage_file)]
    if lane.model and lane.model != 'default':
        argv += ['-m', lane.model]
    if lane.effort and lane.effort != 'default':
        argv += ['--reasoning', lane.effort]
    return argv + ['-z', text]


def read_usage(usage_file, record):
    try:
        report = json.loads(Path(usage_file).read_text())
    except (OSError, ValueError):
        report = None
    reported = isinstance(report, dict)  # Hermes wrote its report: the run ended in an orderly way
    if not reported:
        report = record.get('usage') if isinstance(record.get('usage'), dict) else {}
    def count(key):
        value = report.get(key)
        return value if type(value) is int and value >= 0 else 0
    usage = {'input_tokens': count('input_tokens'), 'output_tokens': count('output_tokens'),
             'cache_read_input_tokens': count('cache_read_tokens'),
             'cache_creation_input_tokens': count('cache_write_tokens'),
             'reasoning_tokens': count('reasoning_tokens'),
             # None when Hermes reported no count: unknown is not the same as no call made.
             'api_calls': count('api_calls') if type(report.get('api_calls')) is int else None,
             'reported': reported}
    # Hermes wrote its report and it shows no activity of any kind: the run ended before a turn began.
    usage['idle'] = reported and not any(usage[key] for key in (
        'api_calls', 'input_tokens', 'output_tokens', 'reasoning_tokens',
        'cache_read_input_tokens', 'cache_creation_input_tokens'))
    def text(key):
        value = report.get(key) or record.get(key)
        return value if isinstance(value, str) else ''
    return usage, text('model'), text('provider'), text('session_id')


def title_session(manifest, lane, session):
    """Best effort: a human looking at the profile finds the lane's session by name."""
    try:
        subprocess.run([hermes.binary_path(manifest) or hermes.binary(), '-p', lane.profile,
                        'sessions', 'rename', session, lane.name], env=turn_env(manifest, lane),
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def turn_env(manifest, lane):
    env = {key: os.environ[key] for key in ENV_ALLOW if key in os.environ}
    env['PATH'] = hermes.turn_path(manifest)
    # The lane names its profile home; the sender's HOME must not pick a different profile of that name.
    env.update(COORDKIT_AS=lane.name, HERMES_ACCEPT_HOOKS='1',
               HERMES_HOME=str(Path(lane.config_dir).expanduser()))
    return env


def missing_session_line(text, session):
    """`Session not found: <id>` for the session this run tried to resume, or ''.

    The id must be on the line: a crashed turn whose output merely contains the
    phrase did work and must not be mistaken for a resume that never started.
    """
    if not session:
        return ''
    if isinstance(text, bytes):
        text = text[-MAX_RECORD:].decode(errors='replace')
    for line in str(text or '').splitlines():
        cleaned = line.strip()
        if 'session not found' in cleaned.lower() and session in cleaned:
            return cleaned[:300]
    return ''


def run_turn(manifest, lane, text, timeout, session=''):
    """(exit code, terminal record, usage report fields). The child gets its own group so a timeout can end it."""
    resumed = session
    with tempfile.TemporaryDirectory(prefix='coordkit-hermes-') as scratch:
        usage_file = Path(scratch)/'usage.json'
        env = turn_env(manifest, lane)
        # Output goes to files, not pipes: a tool the turn left running may keep a
        # descriptor open, and a pipe would then hold this runner (and the lane lock) with it.
        out_file = Path(scratch)/'result.json'
        err_file = Path(scratch)/'stderr.txt'
        binary = hermes.binary_path(manifest)
        try:
            if not binary:
                raise FileNotFoundError('hermes binary unavailable on the turn PATH')
            with out_file.open('wb') as sink, err_file.open('wb') as err_sink:
                proc = subprocess.Popen(argv_for(binary, lane, text, usage_file, session),
                                        stdin=subprocess.DEVNULL, stdout=sink, stderr=err_sink,
                                        env=env, cwd=lane.cwd, start_new_session=True)
        except OSError as exc:
            # The failure still gets a receipt, so the lane reads as unreachable with a reason.
            usage, model, provider, known = read_usage(usage_file, {})
            return 127, False, {'error': f'launch failed: {exc}'}, usage, model, provider, known
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            # The whole group, both signals: the leader exiting on TERM says nothing about a
            # child that ignores it, and the lane lock is released as soon as this returns.
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    proc.wait(timeout=KILL_GRACE)
                except subprocess.TimeoutExpired:
                    pass
        with out_file.open('rb') as stream:
            stream.seek(max(0, os.fstat(stream.fileno()).st_size - MAX_RECORD))
            out = stream.read()
        try:
            with err_file.open('rb') as stream:
                stream.seek(max(0, os.fstat(stream.fileno()).st_size - MAX_RECORD))
                err = stream.read()
        except OSError:
            err = b''
        if err:
            sys.stderr.write(err.decode(errors='replace'))
            sys.stderr.flush()
        record = {}
        for line in reversed((out or b'')[-MAX_RECORD:].decode(errors='replace').splitlines()):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict) and value.get('type') == 'result':
                record = value
                break
        usage, model, provider, session = read_usage(usage_file, record)
        if not record:
            # A pruned resume exits before any result row and prints this on stderr.
            note = missing_session_line(err, resumed) or missing_session_line(out, resumed)
            if note:
                record = {'type': 'result', 'status': 'failed', 'error': note}
    return proc.returncode, timed_out, record, usage, model, provider, session


def failure_wait(manifest):
    return manifest.bus.wake_debounce_minutes * 60 + 1


def main(argv=None):
    from ..manifest import duration, load
    parser = argparse.ArgumentParser(prog='coordkit.adapters.hermes_turn')
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--lane', required=True)
    parser.add_argument('--key', default='')
    opts = parser.parse_args(argv)
    manifest = load(opts.manifest)
    manifest.writable()
    lane = hermes.lane_for(manifest, opts.lane)
    if lane is None:
        print(f'HERMES_TURN no hermes lane named {opts.lane}', file=sys.stderr)
        return 2
    timeout = duration(lane.turn_timeout or hermes.DEFAULT_TURN_TIMEOUT)
    from .. import deliver
    from ..state import read_json
    attached = read_json(deliver.cursor_path(manifest, lane.name)) is not None
    rounds, failure, retried = 0, 0, 0
    while True:
        failure = 0
        with hermes.turn_lock(lane) as held:
            if held is None:
                return 0  # another runner owns this lane's turns and will see the new rows
            while rounds < MAX_ROUNDS:
                hermes.take_request(lane)
                identity, offset, lines, complete = pending(manifest, lane.name)
                if not lines and not complete:
                    advance(manifest, lane.name, identity, offset)
                    continue  # the cursor moved; keep scanning toward the end of the log
                if rounds and not lines:
                    break
                if not lines and attached:
                    # A replayed wake: its rows were already handed over. No turn, but a
                    # receipt, so the wake reads as served rather than as a vanished runner.
                    now = time.time()
                    hermes.append_receipt(lane, {'at': now, 'ended_at': now, 'lane': lane.name, 'profile': lane.profile,
                                                 'key': opts.key, 'round': 0, 'rows': 0, 'exit': 0, 'ok': True,
                                                 'skipped': 'nothing unread', 'error': '', 'session_id': '',
                                                 'model': '', 'provider': '', 'usage': {'api_calls': 0}})
                    print(f'HERMES_TURN lane={lane.name} nothing unread; no turn', file=sys.stderr, flush=True)
                    break  # leave through the request check below, like every other exit
                round_number, rounds = rounds, rounds + 1
                advance(manifest, lane.name, identity, offset)
                started = time.time()
                text = prompt(manifest, lane.name, lines, round_number)
                known = hermes.session_id(lane)
                code, timed_out, record, usage, model, provider, session = run_turn(manifest, lane, text, timeout, known)
                if (known and code and not timed_out and usage['idle']
                        and 'session not found' in str(record.get('error', '')).lower()
                        and known in str(record.get('error', ''))):
                    # Hermes names the very session this run tried to resume as gone, and its own
                    # usage report shows no call and no token in or out: the resume never started.
                    # (Hermes reports nulls there, never an explicit zero.) A run with no report,
                    # or with any activity in it, may have done work and is never repeated.
                    print(f'HERMES_TURN lane={lane.name} session {known} did not resume (exit {code}); starting a new one',
                          file=sys.stderr, flush=True)
                    hermes.remember_session(lane, '')
                    known = ''
                    code, timed_out, record, usage, model, provider, session = run_turn(manifest, lane, text, timeout)
                if session and session != known:
                    hermes.remember_session(lane, session)
                    if not known:
                        title_session(manifest, lane, session)
                ok = code == 0 and not timed_out and record.get('status') == 'success'
                error = '' if ok else ('turn_timeout' if timed_out else
                                       ' '.join(str(record.get('error') or f'exit {code}').split())[:300])
                hermes.append_receipt(lane, {'at': started, 'ended_at': time.time(), 'lane': lane.name,
                                             'profile': lane.profile, 'key': opts.key, 'round': round_number,
                                             'rows': len(lines), 'exit': code, 'ok': ok, 'error': error,
                                             'session_id': session, 'model': model, 'provider': provider,
                                             'usage': usage})
                print(f'HERMES_TURN lane={lane.name} round={round_number} rows={len(lines)} '
                      f'ok={ok} exit={code} {error}'.rstrip(), file=sys.stderr, flush=True)
                if not ok:
                    failure = 1 if round_number else (code or 1)
                    break
        if failure and retried < FAILURE_RETRIES and rounds < MAX_ROUNDS:
            # A failed wake keeps its debounce, so for that window senders start nothing and
            # only leave a request. This runner is the one thing that will read it: it waits
            # the window out before looking, whether or not a request exists yet. A sender
            # arriving after that look finds the window over and wakes the lane itself.
            retried += 1
            time.sleep(failure_wait(manifest))
            if hermes.turn_requested(lane):
                continue
            return failure
        # A sender that saw the lock held just before it was released has left a request
        # and started nothing. It is checked only after the release, so none is missed.
        if failure or rounds >= MAX_ROUNDS or not hermes.turn_requested(lane):
            return failure


if __name__ == '__main__':
    sys.exit(main())
