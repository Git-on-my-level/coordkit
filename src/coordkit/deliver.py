"""Bounded, read-only bus delivery for Claude Code hooks. No waiter or bus writes."""
import argparse
import contextlib
import hashlib
import io
import json
import os
from .paths import env_args, python_env
from pathlib import Path
import shlex
import sys
import time

from . import addressed, hooks, questions
from .manifest import ManifestError
from .state import name_key, read_json, write_json

MAX_OUTPUT = 1536  # bytes, including the JSON envelope
MAX_READ = 256 * 1024
MAX_TRANSCRIPT_READ = 64 * 1024
EVENTS = ('PostToolUse', 'UserPromptSubmit')


def cursor_path(manifest, name):
    # Alongside incumbent inbox/wait cursors, but byte offsets are private to this reader.
    return manifest.paths.bus_log.parent/'state'/'cursors'/f'{name_key(name)}.hook'


def note_path(manifest, name):
    return addressed.home(manifest)/'hooks'/f'{name_key(name)}.json'


def clean(value, limit):
    return ' '.join(str(value or '').split())[:limit]


def envelope(event, lines):
    return json.dumps({'hookSpecificOutput': {'hookEventName': event,
                       'additionalContext': '\n'.join(lines)}}, ensure_ascii=False)


def row_line(row, name):
    ref = f' [{clean(row["ref"], 160)}]' if row.get('ref') else ''
    return (f'BUS {row["kind"]} {clean(row["id"], 80)} from {clean(row["frm"], 80)}: '
            f'{clean(row.get("text"), 200)}{ref}\n'
            f'reply: bin/bus post --from {shlex.quote(name)} --re {shlex.quote(row["id"])} '
            f'--to {shlex.quote(row["frm"])} ...')


def deliver(manifest, payload, out=None):
    """Print before committing delivered offsets. Excess rows remain for the next hook.

    Read at most 256 KiB per invocation; incomplete trailing lines are retried. A corrupt
    complete row fails open without moving past it. Hooks never call addressed role liveness
    (which can probe processes or read the entire bus).
    """
    out = sys.stdout if out is None else out
    event = payload.get('hook_event_name')
    if event not in EVENTS:
        return
    name, how = questions.asker_for(manifest, payload.get('session_id'), payload.get('transcript_path'),
                                   transcript_max_bytes=MAX_TRANSCRIPT_READ)
    if how == 'unknown':
        return
    cursor = cursor_path(manifest, name)
    with addressed.locked(cursor.with_suffix('.hook.lock'), blocking=False) as held:
        if held is None:
            return
        prior = read_json(cursor)
        lines = []
        targets = {name, 'all'} | {role for role, members in addressed.roles(manifest).items() if name in members}
        with manifest.paths.bus_log.open('rb') as stream:
            stat = os.fstat(stream.fileno())
            identity = [stat.st_dev, stat.st_ino]
            if prior is None:
                # First attachment observes only future traffic, never historical asks.
                start = stat.st_size
            else:
                start = prior.get('offset', 0) if prior.get('file') == identity else 0
            if not isinstance(start, int) or not 0 <= start <= stat.st_size:
                start = 0
            stream.seek(start)
            data = stream.read(MAX_READ)
        offset = start
        for raw in data.splitlines(keepends=True):
            if not raw.endswith(b'\n'):
                break
            row = json.loads(raw)
            kind = row.get('kind')
            actionable = kind in ('ask', 'ruling', 'handoff', 'blocker') or (
                kind == 'msg' and (row.get('priority') == 'urgent' or row.get('urgent') is True))
            if actionable and row.get('frm') != name and (row.get('to') in targets or row.get('owner') == name):
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
            offset += len(raw)
        # A successful empty read also proves the hook is attached. Retain the session's
        # explicit transcript binding for listening(); do not manufacture a waiter lease.
        note = {'name': name, 'at': time.time(), 'via': 'hook', 'session_id': payload.get('session_id'),
                'transcript_path': payload.get('transcript_path')}
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
        write_json(cursor, {'file': identity, 'offset': offset})


def run_hook(manifest, stdin=None, out=None):
    try:
        deliver(manifest, json.loads(sys.stdin.read() if stdin is None else stdin), out)
    except BaseException:
        pass  # a broken hook must never interrupt the agent
    return 0


def receipt_path(manifest, target):
    tag = hashlib.sha256(str(Path(target).expanduser().resolve()).encode()).hexdigest()[:20]
    return manifest.paths.root/f'hook-delivery-{tag}.json'


def command(manifest):
    return shlex.join([*env_args(),
                       manifest.launchd.python, '-m', 'coordkit.cli', '--manifest', str(manifest.file),
                       'deliver', '--hook'])


def install(manifest, target):
    manifest.writable()
    target = Path(target).expanduser().resolve()
    receipt = receipt_path(manifest, target)
    record = read_json(receipt, {})
    if record and not record.get('restored'):
        raise ManifestError('hook_receipt_exists: uninstall delivery hooks first')
    before = read_json(target, {})
    after = json.loads(json.dumps(before))
    for groups in after.get('hooks', {}).values():
        for group in groups:
            if any(' deliver --hook' in h.get('command', '') for h in group.get('hooks', [])):
                raise ManifestError('delivery_hooks_present: inspect before installing again')
    for event in EVENTS:
        after.setdefault('hooks', {}).setdefault(event, []).append(
            {'hooks': [{'type': 'command', 'command': command(manifest)}]})
    write_json(receipt, {'target': str(target), 'before': before, 'after': after, 'restored': False})
    write_json(target, after)


def uninstall(manifest, target):
    manifest.writable()
    target = Path(target).expanduser().resolve()
    receipt = receipt_path(manifest, target)
    record = read_json(receipt)
    if not record:
        return
    current = read_json(target, {})
    restored = hooks.reverse(record, current)
    write_json(target, restored)
    record['restored'] = True
    write_json(receipt, record)


def main(argv, manifest):
    parser = argparse.ArgumentParser(prog='coordkit deliver')
    parser.add_argument('action', nargs='?', choices=('install', 'uninstall'))
    parser.add_argument('--hook', action='store_true')
    parser.add_argument('--settings')
    opts = parser.parse_args(argv)
    if opts.hook and not opts.action:
        return run_hook(manifest)
    if not opts.action or not opts.settings or opts.hook:
        parser.error('use --hook, or install/uninstall --settings PATH')
    (install if opts.action == 'install' else uninstall)(manifest, opts.settings)
    return 0


def bootstrap(argv):
    """Fail open even before loading a missing/invalid manifest, without loading lifecycle."""
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            from .manifest import load
            parser = argparse.ArgumentParser()
            parser.add_argument('--manifest')
            parser.add_argument('command')
            opts, rest = parser.parse_known_args(argv)
            return main(rest, load(opts.manifest))
    except BaseException:
        return 0
