"""Wake an interactive (held) Claude Code session through a resident helper in its profile.

A held session cannot be resumed headlessly and coordkit cannot type into it. A
peer session in the same config profile can deliver a message with its own
SendMessage tool. The adapter writes one request file per wake under
``state/wakes/peer/<profile>/``. The helper session runs ``coordkit wake-helper``
as a tracked background task: that command claims requests and prints them, the
helper delivers each with SendMessage, then records the result with ``--ack``.

No helper heartbeat for the profile means the adapter reports ``unavailable``
and writes nothing, so a profile without a helper behaves as before.
"""
import json
import os
import time
from pathlib import Path

from . import sessions
from .state import name_key, read_json, write_json

HELPER_FRESH_SECONDS = 15 * 60  # spans one helper turn between waiter runs


def root(manifest, profile):
    return manifest.paths.bus_log.parent/'state/wakes/peer'/name_key(profile)


def heartbeat_path(manifest, profile):
    return root(manifest, profile)/'helper.json'


def helper_alive(manifest, profile, at=None, is_alive=sessions.alive):
    beat = read_json(heartbeat_path(manifest, profile), {}) or {}
    at = time.time() if at is None else at
    return (type(beat.get('pid')) is int and is_alive(beat['pid'])
            and type(beat.get('at')) in (int, float) and 0 <= at - beat['at'] < HELPER_FRESH_SECONDS)


def target(manifest, name, rows=None):
    """A manifest lane's live session: its pinned session_id, else its tmux session name.

    Only manifest lanes are eligible, so a bus name never reaches an arbitrary session.
    """
    lane = next((lane for lane in manifest.lanes if lane.name == name), None)
    if lane is None:
        return None, f'not_a_manifest_lane: {name}'
    rows = sessions.discover() if rows is None else rows
    if lane.session_id:
        matches = [r for r in rows if r.get('session_id') == lane.session_id]
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, 'ambiguous_session: ' + ', '.join(sessions.key(r) for r in matches)
    matches = [r for r in rows if sessions.tmux_session(r) == name]
    if len(matches) == 1:
        return matches[0], None
    return None, ('ambiguous_session: ' + ', '.join(sessions.key(r) for r in matches)) if matches else f'no_session: {name}'


def prompt(name, rows):
    ids = [r.get('id') for r in rows if r.get('to') == name and r.get('id')]
    return (f'Coordkit wake for lane {name}: {len(ids)} addressed message(s) waiting'
            + (f' ({", ".join(ids[:8])})' if ids else '') + '. Read your bus inbox, handle pending work, '
            f'reply on the bus, then re-arm your bus wait --to {name} as a tracked background task (never with &).')


class ClaudePeer:
    name = 'claude-peer'

    def __init__(self, discover=None, is_alive=sessions.alive):
        self.discover, self.is_alive = discover or sessions.discover, is_alive

    def listening(self, manifest, name, rows):
        session, _ = target(manifest, name, self.discover())
        return bool(session and session.get('status') == 'busy')

    def wake(self, manifest, name, rows, key):
        session, error = target(manifest, name, self.discover())
        if session is None:
            return {'status': 'unavailable', 'reason': error}
        if session.get('status') == 'busy':
            return {'status': 'listening', 'evidence': 'session busy', 'session': sessions.key(session)}
        profile = session['profile']
        if not helper_alive(manifest, profile, is_alive=self.is_alive):
            return {'status': 'unavailable', 'reason': f'no_helper: {profile}', 'session': sessions.key(session)}
        path = root(manifest, profile)/'pending'/f'{name_key(key)}.json'
        if not path.exists():
            write_json(path, {'key': key, 'lane': name, 'profile': profile, 'pid': session['pid'],
                              'session_id': session.get('session_id'), 'target': session.get('name'),
                              'text': prompt(name, rows), 'at': time.time()})
        return {'status': 'requested', 'session': sessions.key(session), 'request': str(path)}


def beat(manifest, profile, pid):
    write_json(heartbeat_path(manifest, profile), {'pid': pid, 'at': time.time()})


def claim(manifest, profile):
    """Move every pending request to claimed/ and return them, oldest first."""
    base = root(manifest, profile)
    claimed = []
    for path in sorted((base/'pending').glob('*.json'), key=lambda p: p.stat().st_mtime):
        destination = base/'claimed'/path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(path, destination)
        except FileNotFoundError:
            continue
        record = read_json(destination, None)
        if isinstance(record, dict):
            claimed.append(record)
    return claimed


def ack(manifest, profile, key, result, reason=''):
    base = root(manifest, profile)
    source = base/'claimed'/f'{name_key(key)}.json'
    record = read_json(source, None)
    if not isinstance(record, dict):
        raise FileNotFoundError(f'unclaimed_request: {key}')
    record.update(result=result, reason=reason, acked_at=time.time())
    write_json(base/'done'/source.name, record)
    source.unlink()
    with (base/'results.jsonl').open('a') as stream:
        stream.write(json.dumps(record) + '\n')
    return record


def read_receipt(record, rows=None):
    """'read' once the target session has gone busy since the ack, else 'sent'."""
    rows = sessions.discover() if rows is None else rows
    session = next((r for r in rows if r.get('session_id') == record.get('session_id')), None)
    if record.get('result') != 'sent':
        return record.get('result') or 'pending'
    return 'read' if session and session.get('status') == 'busy' else 'sent'


def show(record):
    return (f"WAKE_REQUEST key={record['key']} lane={record['lane']} target={record.get('target')} "
            f"session={record.get('session_id')} pid={record.get('pid')}\n{record['text']}")


def owner_session(profile, home=None):
    """The helper's own Claude Code pid: the nearest ancestor registered in this profile."""
    import subprocess
    registered = {r['pid'] for r in sessions.discover(home) if r['profile'] == profile}
    pid = os.getppid()
    for _ in range(32):
        if pid in registered:
            return pid
        if pid <= 1:
            break
        out = subprocess.run(['ps', '-o', 'ppid=', '-p', str(pid)], capture_output=True, text=True, timeout=2).stdout.strip()
        if not out.isdecimal():
            break
        pid = int(out)
    return None


def main(argv, manifest):
    from .ops import parser_for
    options = parser_for('wake-helper').parse_args(argv)
    profile = options.profile
    if options.ack:
        record = ack(manifest, profile, options.ack, options.result, options.reason or '')
        print(f"WAKE_ACK {record['key']} {record['result']}")
        return 0
    owner = owner_session(profile)
    if owner is None:
        print(f'refused: wake-helper must run inside a Claude Code session of profile {profile}')
        return 2
    deadline = time.monotonic() + options.timeout * 60
    while True:
        beat(manifest, profile, owner)
        claimed = claim(manifest, profile)
        if claimed:
            for record in claimed:
                print(show(record))
            print(f'Deliver each WAKE_REQUEST text with SendMessage to its target session, then run '
                  f'coordkit wake-helper --profile {profile} --ack KEY --result sent|failed. '
                  'If the target name matches zero or several sessions, ack failed with the reason. '
                  'Re-arm this command afterwards.')
            return 0
        if time.monotonic() >= deadline:
            print(f'WAKE_HELPER timeout {profile}')
            return 3
        time.sleep(options.poll)
