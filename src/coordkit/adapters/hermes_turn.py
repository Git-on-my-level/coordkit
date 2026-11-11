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
MAX_LINES = 20
MAX_RECORD = 1024 * 1024
KILL_GRACE = 10


def pending(manifest, name):
    """Addressed rows after the shared delivery cursor, and the offset that consumes them."""
    from .. import deliver
    identity, start, data = deliver.unread_batch(manifest, name)
    targets = deliver.targets_for(manifest, name)
    lines, offset = [], start
    for size, row in deliver.parsed_rows(data):
        if row is not None and deliver.addressed_row(row, name, targets):
            if len(lines) >= MAX_LINES:
                break
            lines.append(deliver.row_line(row, name))
        offset += size
    return identity, offset, lines


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


def argv_for(lane, text, usage_file, session):
    """One gateway-free turn. A one-shot run cannot create a titled session, so the first
    turn starts a new one and later turns resume the id it reported."""
    # The profile's hooks are the operator's own config; without consent Hermes skips them.
    argv = [hermes.binary(), '-p', lane.profile, '--in', lane.cwd, '--no-restore-cwd', '--accept-hooks']
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
    if not isinstance(report, dict):
        report = record.get('usage') if isinstance(record.get('usage'), dict) else {}
    def count(key):
        value = report.get(key)
        return value if type(value) is int and value >= 0 else 0
    usage = {'input_tokens': count('input_tokens'), 'output_tokens': count('output_tokens'),
             'cache_read_input_tokens': count('cache_read_tokens'),
             'cache_creation_input_tokens': count('cache_write_tokens'),
             'reasoning_tokens': count('reasoning_tokens'), 'api_calls': count('api_calls')}
    def text(key):
        value = report.get(key) or record.get(key)
        return value if isinstance(value, str) else ''
    return usage, text('model'), text('provider'), text('session_id')


def title_session(lane, session):
    """Best effort: a human looking at the profile finds the lane's session by name."""
    try:
        subprocess.run([hermes.binary(), '-p', lane.profile, 'sessions', 'rename', session, lane.name],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def run_turn(lane, text, timeout, session=''):
    """(exit code, terminal record, usage report fields). The child gets its own group so a timeout can end it."""
    with tempfile.TemporaryDirectory(prefix='coordkit-hermes-') as scratch:
        usage_file = Path(scratch)/'usage.json'
        env = dict(os.environ, COORDKIT_AS=lane.name, HERMES_ACCEPT_HOOKS='1')
        proc = subprocess.Popen(argv_for(lane, text, usage_file, session), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=sys.stderr, env=env, cwd=lane.cwd,
                                start_new_session=True)
        timed_out = False
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    out, _ = proc.communicate(timeout=KILL_GRACE)
                    break
                except subprocess.TimeoutExpired:
                    continue
            else:
                out, _ = proc.communicate()
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
    return proc.returncode, timed_out, record, usage, model, provider, session


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
    with hermes.turn_lock(lane) as held:
        if held is None:
            return 0  # another runner owns this lane's turns and will see the new rows
        for round_number in range(MAX_ROUNDS):
            identity, offset, lines = pending(manifest, lane.name)
            if round_number and not lines:
                break
            advance(manifest, lane.name, identity, offset)
            started = time.time()
            text = prompt(manifest, lane.name, lines, round_number)
            known = hermes.session_id(lane)
            code, timed_out, record, usage, model, provider, session = run_turn(lane, text, timeout, known)
            if known and code and not timed_out and not record:
                # No terminal record means the run never started a turn, which is what a
                # deleted or pruned session looks like. Start a new session once.
                print(f'HERMES_TURN lane={lane.name} session {known} did not resume (exit {code}); starting a new one',
                      file=sys.stderr, flush=True)
                hermes.remember_session(lane, '')
                known = ''
                code, timed_out, record, usage, model, provider, session = run_turn(lane, text, timeout)
            if session and session != known:
                hermes.remember_session(lane, session)
                if not known:
                    title_session(lane, session)
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
                return 1 if round_number else (code or 1)
    return 0


if __name__ == '__main__':
    sys.exit(main())
