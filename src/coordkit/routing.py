"""Roster routing, private runtime identity, and advisory locks."""
import contextlib
import fcntl
from .state import read_json


def cfg(manifest):
    return manifest.addressed


def home(manifest):
    return manifest.paths.root/'state'/'addressed'


def registry(manifest):
    """Manifest agents plus runtime registrations; manifest policy stays authoritative."""
    out = {name: dict(spec, name=name, source='manifest') for name, spec in cfg(manifest).agents.items()}
    for name, spec in (read_json(home(manifest)/'register.json', {}) or {}).items():
        out.setdefault(name, dict(spec, name=name, source='register'))
    return out


def roles(manifest):
    """{role: [members in fallback order]}: the manifest order, then registered agents with that role."""
    out = {role: list(spec['members'] if isinstance(spec, dict) else spec)
           for role, spec in cfg(manifest).roles.items()}
    for name, spec in registry(manifest).items():
        if spec.get('role'):
            members = out.setdefault(spec['role'], [])
            if name not in members:
                members.append(name)
    return out


@contextlib.contextmanager
def locked(path, blocking=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield None
            return
        try:
            yield stream
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)




def waiter_evidence(manifest, name, at=None):
    """Bounded evidence only: kernel wait lock or a waiter-refreshed wait cursor."""
    import time
    from .state import name_key
    at = time.time() if at is None else at
    directory = manifest.paths.bus_log.parent/'state/cursors'
    lock = directory/(name_key(name) + '.wait.lock')
    if lock.exists():
        with lock.open('a') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True, 'armed bus wait lock'
    for base in {directory, manifest.paths.root/'state/cursors'}:
        cursor = base/(name_key(name) + '.wait')
        try:
            age = at - cursor.stat().st_mtime
            with cursor.open() as stream:
                position = stream.read(64).strip()
            if position.isdecimal() and 0 <= age <= manifest.deliver.waiter_fresh_seconds:
                return True, 'fresh bus wait cursor'
        except FileNotFoundError:
            pass
    return False, 'no armed wait lock or fresh wait cursor'


def listening(manifest, name, *args, **kwargs):
    """Compatibility lookup; ordinary hook delivery never imports this engine."""
    if kwargs.pop('waiter_only', False):
        return waiter_evidence(manifest, name, *args, **kwargs)
    from .addressed import listening as observe
    return observe(manifest, name, *args, **kwargs)


# Host metadata belongs only in local state and the authenticated roster projection.
IDENTITY_TEXT = {'config_dir', 'session_id', 'transcript_path', 'pid_started', 'permission_mode', 'cwd_hint'}
IDENTITY_KEYS = IDENTITY_TEXT | {'pid', 'refreshed_at'}


def clean_identity(value):
    import math
    out = {k: v[:4096] for k, v in value.items() if k in IDENTITY_TEXT and isinstance(v, str) and v}
    if type(value.get('pid')) is int and value['pid'] > 0 and out.get('pid_started'):
        out['pid'] = value['pid']
    if type(value.get('refreshed_at')) in (int, float) and math.isfinite(value['refreshed_at']):
        out['refreshed_at'] = value['refreshed_at']
    return out


def internal_name(manifest, name):
    import re
    return (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', name)
            or name in {'all', 'coordkit', 'lead', manifest.mcp.identity, *cfg(manifest).internal_senders,
                        *manifest.escalation.internal_senders})


def identity_sources(manifest, name, env=None, *, stored=None):
    """Merge explicit bindings, lowest priority first; never persist arbitrary env."""
    import os
    from pathlib import Path
    from .identity import home as questions_home
    from .state import name_key
    from .usage import transcript_dir
    env = os.environ if env is None else env
    sessions = read_json(questions_home(manifest)/'sessions.json', {}) or {}
    bindings = [dict(binding, session_id=session) for session, binding in sessions.items()
                if isinstance(binding, dict) and binding.get('name') == name]
    # Multiple sessions for one name are not an unambiguous identity.
    binding = bindings[0] if len(bindings) == 1 else {}
    if binding and not binding.get('transcript_path'):
        candidates = transcript_paths(manifest, name)
        if len(candidates) == 1:
            binding['transcript_path'] = str(next(iter(candidates)))
    post = {k: env.get(v) for k, v in (('config_dir', 'CLAUDE_CONFIG_DIR'),
                                     ('session_id', 'CLAUDE_CODE_SESSION_ID'))}
    cwd = os.getcwd()
    matching = [lane for lane in manifest.lanes if str(Path(lane.cwd).expanduser().resolve()) == cwd]
    if any(lane.name == name for lane in matching):
        post['cwd_hint'] = cwd
    note = read_json(home(manifest)/'hooks'/f'{name_key(name)}.json', {}) or {}
    if note.get('name') != name:
        note = {}
    lane = next((lane for lane in manifest.lanes if lane.name == name), None)
    declared = dict(config_dir=lane.config_dir, cwd_hint=lane.cwd, session_id=lane.session_id) if lane else {}
    if lane and lane.session_id:
        declared['transcript_path'] = str(transcript_dir(lane)/f'{lane.session_id}.jsonl')
    merged = {}
    for source in (binding, stored or {}, post, note, declared):
        source = clean_identity(source)
        if not source.get('config_dir') and source.get('transcript_path'):
            for parent in Path(source['transcript_path']).expanduser().parents:
                if parent.name == 'projects':
                    source['config_dir'] = str(parent.parent)
                    break
        # Do not attach a lower-priority session's path/PID to a new session.
        if source.get('session_id') and merged.get('session_id') != source['session_id']:
            for key in ('config_dir', 'transcript_path', 'pid', 'pid_started', 'permission_mode', 'cwd_hint'):
                merged.pop(key, None)
        if any(source.get(key) and source[key] != merged.get(key) for key in ('config_dir', 'transcript_path')):
            merged.pop('pid', None)
            merged.pop('pid_started', None)
        merged.update(source)
    merged.pop('refreshed_at', None)
    return clean_identity(merged)


def transcript_paths(manifest, name, identity=None):
    """Exact hook/session paths used by non-lane model observations."""
    from pathlib import Path
    from .identity import home as questions_home
    from .state import name_key
    paths = set()
    note = read_json(home(manifest)/'hooks'/f'{name_key(name)}.json', {}) or {}
    sessions = read_json(questions_home(manifest)/'sessions.json', {}) or {}
    for record in [identity or {}, note if note.get('name') == name else {},
                   *(b for b in sessions.values() if isinstance(b, dict) and b.get('name') == name)]:
        if isinstance(record.get('transcript_path'), str) and record['transcript_path']:
            paths.add(Path(record['transcript_path']).expanduser())
    import re
    for session, binding in sessions.items():
        if isinstance(binding, dict) and binding.get('name') == name and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', session):
            for directory in manifest.questions.config_dirs:
                paths.update((Path(directory).expanduser()/'projects').glob(f'*/{session}.jsonl'))
    return paths


def identity_pid(record):
    """Reject missing/dead/reused PIDs using their captured process start time."""
    import subprocess
    from .adapters import claude_resume
    try:
        if record.get('pid') and record.get('pid_started') == claude_resume.start_time(record['pid']):
            return record['pid']
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def probe_identity(record):
    import subprocess
    from .adapters import claude_resume
    from .process import ToolError
    out = dict(record)
    for key in ('pid', 'pid_started'):
        out.pop(key, None)
    from pathlib import Path
    if not out.get('transcript_path') or not Path(out['transcript_path']).is_file():
        return out
    try:
        result = claude_resume.probe([claude_resume.lsof_binary(), '-nP', '-t', '--', out['transcript_path']])
        pids = result.stdout.split()
        if result.returncode == 0 and not result.stderr.strip() and pids and all(p.isdecimal() for p in pids):
            pid = min(map(int, pids))
            started = claude_resume.start_time(pid)
            if started:
                out.update(pid=pid, pid_started=started)
    except (OSError, subprocess.SubprocessError, ToolError):
        pass
    return out


def _store_identity(manifest, name, via, identity, *, register=False):
    """Persist an identity selected while the caller holds register.lock."""
    import time
    from .state import update_json
    at = time.time()
    def transform(current):
        entry = current.setdefault(name, {})
        if register:
            entry.setdefault('registered_at', at)
            entry.setdefault('registered_by', via)
        entry['identity'] = clean_identity(dict(identity, refreshed_at=at))
        entry['refreshed_at'] = at
        return current
    return update_json(home(manifest)/'register.json', transform)[name]


def write_register(manifest, name, via, identity=None, *, env=None):
    """First local post/ack/wait makes a name routable; manifest policy wins."""
    if internal_name(manifest, name):
        return
    with locked(home(manifest)/'register.lock'):
        previous = ((read_json(home(manifest)/'register.json', {}) or {}).get(name) or {}).get('identity') or {}
        record = identity_sources(manifest, name, env=env, stored=previous) if identity is None else clean_identity(identity)
        return _store_identity(manifest, name, via, probe_identity(record), register=name not in cfg(manifest).agents)


def register_activity(manifest, name, via, *, env=None):
    """Optional metadata cannot fail an otherwise successful post, ack or wait."""
    import logging
    try:
        return write_register(manifest, name, via, env=env)
    except Exception as exc:
        logging.getLogger(__name__).warning('optional %s registration for %s failed (%s)', via, name, type(exc).__name__)


def refresh_identity(manifest, name):
    """Hook heartbeat, bounded and throttled; does not register never-seen names."""
    import time
    from .manifest import duration
    if internal_name(manifest, name):
        return
    with locked(home(manifest)/'register.lock'):
        entry = (read_json(home(manifest)/'register.json', {}) or {}).get(name)
        if entry is None and name not in cfg(manifest).agents:
            return
        previous = clean_identity((entry or {}).get('identity') or {})
        record = identity_sources(manifest, name, stored=previous)
        comparable = {k: v for k, v in previous.items() if k not in {'pid', 'pid_started', 'refreshed_at'}}
        changed = {k: v for k, v in record.items() if k not in {'pid', 'pid_started'}} != comparable
        if 0 <= time.time() - previous.get('refreshed_at', 0) < duration(cfg(manifest).heartbeat_every):
            return
        if not changed and identity_pid(previous) is not None:
            return
        return _store_identity(manifest, name, 'hook', probe_identity(record))


def roster_identity(manifest, name):
    """Read-only projection: stored heartbeat plus authoritative manifest identity."""
    record = clean_identity(((read_json(home(manifest)/'register.json', {}) or {}).get(name) or {}).get('identity') or {})
    # Reading the roster never captures the MCP server's environment as lane identity.
    observed = identity_sources(manifest, name, env={}, stored=record)
    if record.get('refreshed_at') is not None:
        observed['refreshed_at'] = record['refreshed_at']
    return observed
