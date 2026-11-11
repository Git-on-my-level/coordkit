"""Hermes Agent lanes: one named session per profile, run one turn at a time.

A Hermes lane has no process between turns, so it cannot arm a waiter. Coordkit
starts each turn (`hermes -z` into the lane's named session), and the turn runner
starts another when addressed rows arrived meanwhile. Liveness is the runner's
kernel lock; usage is the per-turn report Hermes writes for every run.
"""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from ..process import ToolError

REFUSAL_SECONDS = 5
DEFAULT_TURN_TIMEOUT = '2h'
RECEIPT_TAIL = 2 * 1024 * 1024
HOOK_MARKER = 'deliver --hook'
FAILED = ('failed', 'unresumable', 'unavailable')


def binary():
    return os.environ.get('HERMES', 'hermes')


def lane_for(manifest, name):
    return next((lane for lane in getattr(manifest, 'lanes', ())
                 if lane.name == name and getattr(lane, 'harness', '') == 'hermes'), None)


def state_dir():
    state = Path(os.environ.get('XDG_STATE_HOME') or Path.home()/'.local/state').expanduser()
    if not state.is_absolute():
        state = Path.home()/'.local/state'
    return state/'coordkit/hermes'


def lane_key(lane):
    """One identity per profile home and lane name, shared by every manifest for this user."""
    home = str(Path(lane.config_dir).expanduser().resolve())
    return hashlib.sha256(json.dumps([home, lane.name]).encode()).hexdigest()[:32]


def lock_path(lane):
    return state_dir()/f'{lane_key(lane)}.turn.lock'


def receipts_path(lane):
    return state_dir()/f'{lane_key(lane)}.turns.jsonl'


def session_path(lane):
    return state_dir()/f'{lane_key(lane)}.session.json'


def session_id(lane):
    """The lane's current Hermes session, as its last turn reported it."""
    from ..state import read_json
    try:
        value = (read_json(session_path(lane), {}) or {}).get('session_id')
    except (ValueError, UnicodeError, AttributeError):
        return ''
    return value if isinstance(value, str) else ''


def remember_session(lane, value):
    from ..state import write_json
    if value:
        write_json(session_path(lane), {'session_id': value, 'lane': lane.name, 'profile': lane.profile})
    else:
        session_path(lane).unlink(missing_ok=True)


@contextlib.contextmanager
def turn_lock(lane):
    """Held by the runner for its whole life; the kernel releases it if the runner dies."""
    path = lock_path(lane)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        try:
            yield stream
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def turn_running(lane):
    if not lock_path(lane).exists():
        return False
    with turn_lock(lane) as held:
        return held is None


def append_receipt(lane, receipt):
    path = receipts_path(lane)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    with os.fdopen(fd, 'a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(json.dumps(receipt) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def receipts(lane):
    """Complete rows from a bounded tail, oldest first. (rows, invalid, unreadable)."""
    from ..usage import tail_lines
    try:
        lines = tail_lines(receipts_path(lane), RECEIPT_TAIL)
    except FileNotFoundError:
        return [], 0, 0
    except OSError:
        return [], 0, 1
    rows, invalid = [], 0
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            row = None
        if (isinstance(row, dict) and type(row.get('ended_at')) in (int, float)
                and isinstance(row.get('usage', {}), dict)):
            rows.append(row)
        else:
            invalid += 1
    return rows, invalid, 0


def last_receipt(lane):
    rows, _, _ = receipts(lane)
    return rows[-1] if rows else None


def settled(manifest, name, record):
    """The turn that a wake started has finished cleanly, so the debounce no longer protects anything."""
    lane = lane_for(manifest, name)
    if lane is None or record.get('status') != 'woken' or turn_running(lane):
        return False
    last = last_receipt(lane)
    return bool(last and last.get('ok') and last['ended_at'] >= record.get('at', float('inf')))


def reachable(manifest, name):
    """(live, evidence) for an on-demand lane: a turn can be started unless the last wake could not."""
    from ..wake import wake_record
    lane = lane_for(manifest, name)
    if turn_running(lane):
        return True, 'hermes turn running'
    if not shutil.which(binary()):
        return False, 'hermes binary unavailable'
    record = wake_record(manifest, name)
    if record.get('status') in FAILED:
        reason = next((a.get('reason') for a in reversed(record.get('attempts', []))
                       if a.get('adapter') == 'hermes' and a.get('reason')), record['status'])
        return False, 'hermes wake ' + record['status'] + ': ' + ' '.join(str(reason).split())[:160]
    last = last_receipt(lane)
    if last and not last.get('ok') and last['ended_at'] >= record.get('at', 0):
        return False, 'hermes turn failed: ' + ' '.join(str(last.get('error') or last.get('exit')).split())[:160]
    return True, 'hermes wake-on-demand lane'


def hook_installed(lane):
    """Textual check only: the profile config belongs to Hermes."""
    try:
        return HOOK_MARKER in (Path(lane.config_dir).expanduser()/'config.yaml').read_text(errors='replace')
    except OSError:
        return False


HOOK_BEGIN = '# coordkit-delivery begin (managed by `coordkit hooks install --hermes-lane`)'
HOOK_END = '# coordkit-delivery end'


def hook_block(manifest):
    """Turn-start delivery for turns coordkit did not start: addressed rows join the user message."""
    from ..deliver import command
    return (f'{HOOK_BEGIN}\nhooks:\n  pre_llm_call:\n  - command: {json.dumps(command(manifest))}\n'
            f'    timeout: 10\n{HOOK_END}\n')


def manage_hook(manifest, name, action):
    """Own one marked block at the end of the profile's config.yaml, and nothing else in it.

    Coordkit has no YAML parser, so it never edits an existing `hooks:` map: with one
    present, install refuses and prints the entry to add by hand.
    """
    import re
    from ..errors import ManifestError
    lane = lane_for(manifest, name)
    if lane is None:
        raise ManifestError('not_a_hermes_lane: ' + name)
    config = Path(lane.config_dir).expanduser()/'config.yaml'
    text = config.read_text() if config.exists() else ''
    start, end = text.find(HOOK_BEGIN), text.find(HOOK_END)
    owned = start >= 0 and end > start
    if action == 'status':
        return ('installed coordkit-owned' if owned else
                'installed by hand' if HOOK_MARKER in text else 'absent')
    manifest.writable()
    rest = (text[:start] + text[end + len(HOOK_END):].lstrip('\n')) if owned else text
    if action == 'install':
        if rest.lstrip().startswith('{') or re.search(r'^hooks\s*:', rest, re.M):
            raise ManifestError('hermes_hooks_present: add this under hooks.pre_llm_call in ' + str(config)
                                + ' yourself:\n' + hook_block(manifest))
        rest = rest + ('' if not rest or rest.endswith('\n') else '\n') + hook_block(manifest)
    elif not owned:
        return 'absent'
    temporary = config.with_name(config.name + '.coordkit.tmp')
    fd = os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(rest)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, config)
    return 'OK ' + action


class Hermes:
    name = 'hermes'

    def __init__(self, manifest=None):
        self.manifest = manifest

    def lanes(self):
        return [lane for lane in getattr(self.manifest, 'lanes', ()) if getattr(lane, 'harness', '') == 'hermes']

    def status(self):
        lanes = self.lanes()
        found = shutil.which(binary())
        if not lanes:
            return 'no hermes lanes; binary ' + ('found' if found else 'absent (optional)')
        if not found:
            raise ToolError('hermes', 6, 'binary_unavailable: hermes lanes are declared')
        missing = [lane.name for lane in lanes if not Path(lane.config_dir).expanduser().is_dir()]
        if missing:
            raise ToolError('hermes', 6, 'profile_home_missing: ' + ','.join(missing))
        unhooked = [lane.name for lane in lanes if not hook_installed(lane)]
        detail = f'lanes={",".join(lane.name for lane in lanes)}'
        # Coordkit-started turns carry their rows in the prompt; the hook only serves
        # turns someone starts in the profile by hand, so its absence is not a fault.
        return detail + (f'; no turn-start hook for {",".join(unhooked)}' if unhooked else '')

    def listening(self, manifest, name, rows):
        lane = lane_for(manifest, name)
        return bool(lane) and turn_running(lane)

    def wake(self, manifest, name, rows, key):
        from ..wake import wake_path
        lane = lane_for(manifest, name)
        if lane is None:
            return {'status': 'unresumable', 'reason': 'no hermes lane'}
        if turn_running(lane):
            return {'status': 'listening', 'evidence': 'hermes turn running'}
        if not shutil.which(binary()):
            return {'status': 'unresumable', 'reason': 'hermes binary unavailable'}
        log = wake_path(manifest, name).with_suffix('.hermes.log')
        log.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        package_root = str(Path(__file__).resolve().parents[2])
        env['PYTHONPATH'] = os.pathsep.join(filter(None, [package_root, env.get('PYTHONPATH')]))
        fd = os.open(log, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'ab') as out:
            proc = subprocess.Popen([sys.executable, '-m', 'coordkit.adapters.hermes_turn',
                                     '--manifest', str(manifest.file), '--lane', name, '--key', key],
                                    stdin=subprocess.DEVNULL, stdout=out, stderr=out, env=env,
                                    start_new_session=True)
        deadline = time.monotonic() + REFUSAL_SECONDS
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(min(.2, max(0, deadline - time.monotonic())))
        if proc.poll():
            with log.open('rb') as stream:
                stream.seek(max(0, os.fstat(stream.fileno()).st_size - 2000))
                detail = stream.read().decode(errors='replace')
            raise ToolError('hermes', proc.returncode, detail)
        return {'status': 'woken', 'profile': lane.profile, 'pid': proc.pid}

    def transcripts(self, manifest):
        return [lane.name for lane in manifest.lanes if getattr(lane, 'harness', '') == 'hermes']
