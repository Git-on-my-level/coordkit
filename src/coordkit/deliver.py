"""Bounded Claude Code delivery and Stop decisions. No waiter or bus writes."""
import argparse
import contextlib
import hashlib
import io
import json
import os
from .paths import env_args
from pathlib import Path
import shlex
import sys
import time

from . import routing as addressed, identity
from .errors import ManifestError
from .state import name_key, read_json, write_json, update_json

MAX_OUTPUT = 1536  # bytes, including the JSON envelope
MAX_READ = 256 * 1024
MAX_TRANSCRIPT_READ = 64 * 1024
HERMES_EVENT = 'pre_llm_call'  # Hermes shell hook, once at turn start: its {"context": ...} answer joins the user message
EVENTS = ('PostToolUse', 'UserPromptSubmit', HERMES_EVENT)
HOOK_EVENTS = ('PostToolUse', 'UserPromptSubmit', 'Stop')  # the Claude settings entries coordkit installs
from .hook_runtime import HOOK_TIMEOUT, hook_deadline


def cursor_path(manifest, name):
    # Alongside incumbent inbox/wait cursors, but byte offsets are private to this reader.
    return manifest.paths.bus_log.parent/'state'/'cursors'/f'{name_key(name)}.hook'


def note_path(manifest, name):
    return addressed.home(manifest)/'hooks'/f'{name_key(name)}.json'


def clean(value, limit):
    return ' '.join(str(value or '').split())[:limit]


def envelope(event, lines):
    if event == HERMES_EVENT:
        return json.dumps({'context': '\n'.join(lines)}, ensure_ascii=False)
    return json.dumps({'hookSpecificOutput': {'hookEventName': event,
                       'additionalContext': '\n'.join(lines)}}, ensure_ascii=False)


def row_line(row, name):
    ref = f' [{clean(row["ref"], 160)}]' if row.get('ref') else ''
    return (f'BUS {row["kind"]} {clean(row["id"], 80)} from {clean(row["frm"], 80)}: '
            f'{clean(row.get("text"), 200)}{ref}\n'
            f'reply: bin/bus post --from {shlex.quote(name)} --re {shlex.quote(row["id"])} '
            f'--to {shlex.quote(row["frm"])} ...')


def targets_for(manifest, name):
    return {name, 'all'} | {role for role, members in addressed.roles(manifest).items() if name in members}


def actionable(row):
    return row.get('kind') in ('ask', 'ruling', 'handoff', 'blocker') or (
        row.get('kind') == 'msg' and (row.get('priority') == 'urgent' or row.get('urgent') is True))


def addressed_row(row, name, targets):
    return actionable(row) and row.get('frm') != name and (row.get('to') in targets or row.get('owner') == name)


def unread_batch(manifest, name):
    """Shared bounded cursor snapshot; a first attachment starts at EOF."""
    prior = read_json(cursor_path(manifest, name))
    with manifest.paths.bus_log.open('rb') as stream:
        stat = os.fstat(stream.fileno())
        identity = [stat.st_dev, stat.st_ino]
        # First attachment observes future traffic only; replacement starts at zero.
        start = stat.st_size if prior is None else (
            prior.get('offset', 0) if prior.get('file') == identity else 0)
        if not isinstance(start, int) or not 0 <= start <= stat.st_size:
            start = 0
        stream.seek(start)
        return identity, start, stream.read(MAX_READ)


def parsed_rows(data):
    for raw in data.splitlines(keepends=True):
        if not raw.endswith(b'\n'):
            break
        try:
            row = json.loads(raw)
            if not isinstance(row, dict) or (actionable(row) and not all(
                    isinstance(row.get(key), str) for key in ('id', 'frm', 'to'))):
                raise ValueError('invalid bus row')
        except ValueError:
            row = None
        yield len(raw), row


def stop_counter_path(manifest, name, session, transcript=None):
    if session:
        key = 'session:' + str(session)
    elif transcript:
        key = 'transcript:' + str(Path(transcript).expanduser().resolve())
    else:
        return None
    tag = hashlib.sha256(key.encode()).hexdigest()[:20]
    return cursor_path(manifest, name).with_name(f'{name_key(name)}.{tag}.stop.json')


def requires_waiter(manifest, name):
    lane = next((lane for lane in manifest.lanes if lane.name == name), None)
    override = getattr(lane, 'stop_requires_waiter', None)
    return manifest.deliver.stop_requires_waiter if override is None else override


def stop_hook(manifest, payload, out=None):
    """Never consume delivery rows or let a busy transcript stand in for a waiter."""
    from .wake import registered, retired
    if payload.get('hook_event_name') != 'Stop' or payload.get('stop_hook_active'):
        return
    name, how = identity.asker_for(manifest, payload.get('session_id'), payload.get('transcript_path'),
                                   transcript_max_bytes=MAX_TRANSCRIPT_READ)
    if how == 'unknown' or retired(manifest, name):
        return
    counter = stop_counter_path(manifest, name, payload.get('session_id'), payload.get('transcript_path'))
    if counter is None:
        return  # no stable identity means no safe way to enforce a block budget
    with addressed.locked(counter.with_suffix('.lock'), blocking=False) as held:
        if held is None:
            return
        _, _, data = unread_batch(manifest, name)
        targets = targets_for(manifest, name)
        count = sum(1 for _, row in parsed_rows(data) if row is not None and addressed_row(row, name, targets))
        reason = None
        if count:
            reason = (f'{count} addressed bus rows are unread: read them with your bus inbox command, '
                      'handle them, then end your turn with a background bus wait armed')
        elif requires_waiter(manifest, name) and name in registered(manifest) and not addressed.listening(
                manifest, name, waiter_only=True)[0]:
            reason = (f'arm a background `bus wait --to {name}` (your usual waiter) before ending the turn '
                      'so addressed work wakes you')
        if reason is None:
            counter.unlink(missing_ok=True)
            return
        at = time.time()
        prior = read_json(counter, {})
        if not 0 <= at - prior.get('at', 0) < 600:
            prior = {'at': at, 'blocks': 0}
        if prior.get('blocks', 0) >= 2:
            return
        write_json(counter, dict(prior, blocks=prior.get('blocks', 0) + 1))
        out = sys.stdout if out is None else out
        out.write(json.dumps({'decision': 'block', 'reason': reason}) + '\n')
        out.flush()


def run_stop_hook(manifest, stdin=None, out=None):
    try:
        with hook_deadline(HOOK_TIMEOUT):
            payload = sys.stdin.read(MAX_READ + 1) if stdin is None else stdin
            if len(payload) <= MAX_READ:
                stop_hook(manifest, json.loads(payload), out)
    except BaseException:
        pass
    return 0


def deliver(manifest, payload, out=None):
    """Print before committing delivered offsets. Excess rows remain for the next hook.

    Read at most 256 KiB per invocation; incomplete trailing lines are retried. A corrupt
    complete row is skipped with a warning and its offset advances. Hooks never call addressed role liveness
    (which can probe processes or read the entire bus).
    """
    out = sys.stdout if out is None else out
    event = payload.get('hook_event_name')
    if event not in EVENTS:
        return
    profile_lane = None
    if event == HERMES_EVENT and not os.environ.get('COORDKIT_AS'):
        # Hermes names the profile in every hook payload, and a profile holds one lane.
        profile_lane = next((lane for lane in manifest.lanes if getattr(lane, 'harness', '') == 'hermes'
                             and getattr(lane, 'profile', '') == payload.get('profile')), None)
    if profile_lane is not None:
        name, how = profile_lane.name, 'profile'
    else:
        name, how = identity.asker_for(manifest, payload.get('session_id'), payload.get('transcript_path'),
                                       transcript_max_bytes=MAX_TRANSCRIPT_READ)
    if how == 'unknown':
        return
    cursor = cursor_path(manifest, name)
    with addressed.locked(cursor.with_suffix('.hook.lock'), blocking=False) as held:
        if held is None:
            return
        file_identity, start, data = unread_batch(manifest, name)
        lines = []
        targets = targets_for(manifest, name)
        offset = start
        invalid = 0
        for size, row in parsed_rows(data):
            if row is None:
                invalid += 1
            elif addressed_row(row, name, targets):
                line = row_line(row, name)
                # Unicode can consume four bytes per character. Bound even a single row's text.
                while len((envelope(event, [line]) + '\n').encode()) > MAX_OUTPUT:
                    text = clean(row.get('text'), 200)
                    if not text:
                        raise ValueError('hook_row_metadata_too_large')
                    row['text'] = text[:-1]
                    line = row_line(row, name)
                if len((envelope(event, lines + [line]) + '\n').encode()) > MAX_OUTPUT:
                    break
                lines.append(line)
            offset += size
        if invalid:
            print(f'BUS_INVALID_JSON skipped={invalid} (hook reader)', file=sys.stderr)
        # A successful empty read also proves the hook is attached. Retain the session's
        # explicit transcript binding for listening(); do not manufacture a waiter lease.
        note = {'name': name, 'at': time.time(), 'via': 'hook', 'session_id': payload.get('session_id'),
                'transcript_path': payload.get('transcript_path'), 'permission_mode': payload.get('permission_mode')}
        note_file = note_path(manifest, name)
        staged = note_file.with_suffix('.pending')
        # Prepare durable bytes before printing, but publish listening evidence only
        # after stdout succeeds. A broken pipe must not make a deaf session look live.
        write_json(staged, note)
        try:
            if lines:
                out.write(envelope(event, lines) + '\n')
                out.flush()
            staged.replace(note_file)
        finally:
            staged.unlink(missing_ok=True)
        # Ignored rows can advance silently; actionable rows advance only after flush succeeds.
        write_json(cursor, {'file': file_identity, 'offset': offset})
        # Optional metadata must never replay an already-flushed delivery on failure.
        from .routing import refresh_identity
        try:
            refresh_identity(manifest, name)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning('optional hook identity for %s failed (%s)', name, type(exc).__name__)


def run_hook(manifest, stdin=None, out=None):
    try:
        deliver(manifest, json.loads(sys.stdin.read() if stdin is None else stdin), out)
    except BaseException:
        pass  # a broken hook must never interrupt the agent
    return 0


def receipt_path(manifest, target):
    tag = hashlib.sha256(str(Path(target).expanduser().resolve()).encode()).hexdigest()[:20]
    return manifest.paths.root/f'hook-delivery-{tag}.json'


def command(manifest, event=None):
    return shlex.join([*env_args(), 'COORDKIT_HOOK_OWNER=coordkit-delivery',
                       manifest.launchd.python, '-m', 'coordkit.cli', '--manifest', str(manifest.file),
                       'deliver', '--stop-hook' if event == 'Stop' else '--hook'])


def install(manifest, target):
    from . import hooks
    manifest.writable()
    target = Path(target).expanduser().resolve()
    receipt = receipt_path(manifest, target)
    record = read_json(receipt, {})
    if record and not record.get('restored'):
        if read_json(target, {}) == record['before']:
            # The receipt is durable before settings rename. Complete a lost
            # initial install using the same owned-entry merge under the lock.
            def prepare_recovery(before, after):
                if read_json(receipt) != record:
                    raise ManifestError('hook_receipt_conflict: delivery installation changed')
            update_json(target, lambda current: hooks.reverse(
                {'before': record['after'], 'after': record['before']}, current),
                before_replace=prepare_recovery)
        hooks.reconcile(manifest)
        return
    expected_receipt = record
    def transform(before):
        after = json.loads(json.dumps(before))
        for groups in after.get('hooks', {}).values():
            for group in groups:
                if any(any(f' deliver {flag}' in h.get('command', '') for flag in ('--hook', '--stop-hook'))
                       for h in group.get('hooks', [])):
                    raise ManifestError('delivery_hooks_present: inspect before installing again')
        for event in HOOK_EVENTS:
            after.setdefault('hooks', {}).setdefault(event, []).append(
                {'hooks': [{'type': 'command', 'command': command(manifest, event)}]})
        return after
    def prepare(before, after):
        nonlocal expected_receipt
        if read_json(receipt, {}) != expected_receipt:
            raise ManifestError('hook_receipt_conflict: delivery installation changed')
        expected_receipt = {'target': str(target), 'before': before, 'after': after, 'restored': False}
        write_json(receipt, expected_receipt)
    update_json(target, transform, before_replace=prepare)


def uninstall(manifest, target):
    from . import hooks
    manifest.writable()
    target = Path(target).expanduser().resolve()
    receipt = receipt_path(manifest, target)
    record = read_json(receipt)
    if not record or record.get('restored'):
        return
    def prepare(before, after):
        if read_json(receipt) != record:
            raise ManifestError('hook_receipt_conflict: delivery installation changed')
    def finish(before, after):
        write_json(receipt, dict(record, restored=True))
    try:
        update_json(target, lambda current: hooks.reverse(record, current),
                    before_replace=prepare, after_replace=finish, require_exists=True)
    except FileNotFoundError:
        # Retire ownership without recreating settings. Serialize with installers
        # and refuse to finalize over a concurrent receipt or reappearing target.
        with addressed.locked(target.with_name(target.name + '.lock')):
            prepare(None, None)
            if target.exists():
                raise ManifestError('settings_update_conflict: missing target reappeared')
            finish(None, None)


def main(argv, manifest):
    parser = argparse.ArgumentParser(prog='coordkit deliver')
    parser.add_argument('action', nargs='?', choices=('install', 'uninstall'))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--hook', action='store_true')
    mode.add_argument('--stop-hook', action='store_true')
    parser.add_argument('--settings')
    opts = parser.parse_args(argv)
    if opts.stop_hook and not opts.action:
        return run_stop_hook(manifest)
    if opts.hook and not opts.action:
        return run_hook(manifest)
    if not opts.action or not opts.settings or opts.hook or opts.stop_hook:
        parser.error('use --hook/--stop-hook, or install/uninstall --settings PATH')
    (install if opts.action == 'install' else uninstall)(manifest, opts.settings)
    return 0


def bootstrap(argv):
    """Fail open even before loading a missing/invalid manifest, without loading lifecycle."""
    try:
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), (hook_deadline(HOOK_TIMEOUT) if '--stop-hook' in argv else contextlib.nullcontext()):
            from .adapters.claude_hook import load
            parser = argparse.ArgumentParser()
            parser.add_argument('--manifest')
            parser.add_argument('command')
            opts, rest = parser.parse_known_args(argv)
            result = main(rest, load(opts.manifest))
        # Surface explicit corrupt-row diagnostics; bootstrap failures stay silent.
        sys.stderr.write(errors.getvalue())
        return result
    except BaseException:
        return 0
