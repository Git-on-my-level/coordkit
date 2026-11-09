"""Conservative process-holder and PID identity probes for Claude resume."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

from ..process import ToolError
from ..state import name_key, read_json

PROBE_TIMEOUT = 2


def lock_path(lane):
    config = lane.config_dir or os.environ.get('CLAUDE_CONFIG_DIR') or Path.home()/'.claude'
    config = str(Path(config).expanduser().resolve())
    key = hashlib.sha256(json.dumps([config, lane.session_id]).encode()).hexdigest()
    state = Path(os.environ.get('XDG_STATE_HOME') or Path.home()/'.local/state').expanduser()
    if not state.is_absolute():
        state = Path.home()/'.local/state'
    return state/'coordkit/resume-locks'/f'{key}.lock'


@contextlib.contextmanager
def session_lock(lane):
    """One lock per Claude identity, shared by every manifest for this user."""
    path = lock_path(lane)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'a') as stream:
        os.fchmod(stream.fileno(), 0o600)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        try:
            yield stream
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def probe(argv):
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            timeout=PROBE_TIMEOUT, env=dict(os.environ, LC_ALL='C'))
    return result


def processes():
    result = probe(['ps', '-axww', '-o', 'pid=,ppid=,stat=,lstart=,args='])
    if result.returncode:
        raise ToolError('ps', result.returncode, 'holder_probe_failed')
    rows = {}
    for line in result.stdout.splitlines():
        fields = line.split(None, 8)
        if (len(fields) != 9 or not fields[0].isdecimal() or not fields[1].isdecimal()
                or 'Z' in fields[2]):
            continue
        rows[int(fields[0])] = {'ppid': int(fields[1]), 'started': ' '.join(fields[3:8]), 'argv': fields[8]}
    if not rows:
        raise ToolError('ps', 6, 'holder_probe_failed')
    return rows


def start_time(pid):
    result = probe(['ps', '-p', str(pid), '-o', 'lstart='])
    return ' '.join(result.stdout.split()) if result.returncode == 0 else None


def read_pid(path):
    """Read child-written PIDs and the old parent-written JSON format."""
    try:
        value = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return None, {}
    record = value if isinstance(value, dict) else {}
    pid = record.get('pid') if record else value
    return (pid if type(pid) is int and pid > 0 else None), record


def pid_matches(pid, record, snapshot):
    current = snapshot.get(pid)
    if current is None:
        return False
    if record.get('pid') == pid and record.get('started'):
        return record['started'] == current['started']
    # After a parent crash there may be no identity sidecar. A bare PID alone
    # must never suppress a lane just because the OS reused it for another tool.
    result = probe(['ps', '-p', str(pid), '-o', 'comm='])
    return result.returncode == 0 and 'claude' in result.stdout.lower()


def transcript_paths(manifest, lane):
    from ..identity import home
    from ..routing import home as addressed_home
    from ..usage import transcript_dir
    paths = {transcript_dir(lane)/f'{lane.session_id}.jsonl'}
    binding = read_json(home(manifest)/'sessions.json', {}).get(lane.session_id, {})
    note = read_json(addressed_home(manifest)/'hooks'/f'{name_key(lane.name)}.json', {})
    for record in (binding, note if note.get('session_id') == lane.session_id else {}):
        if record.get('transcript_path'):
            paths.add(Path(record['transcript_path']).expanduser().resolve())
    return paths


def lsof_binary():
    binary = shutil.which('lsof')
    if binary:
        return binary
    for candidate in ('/usr/sbin/lsof', '/usr/bin/lsof'):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise ToolError('lsof', 6, 'lsof_unavailable')


def holder_present(manifest, lane, snapshot):
    if any(lane.session_id in row['argv'] for row in snapshot.values()):
        return True
    return bool(transcript_holders(manifest, lane))


def transcript_holders(manifest, lane):
    """Return exact open-file PIDs as ancestry roots, including interactive sessions without --resume."""
    holders = set()
    lsof = lsof_binary()
    for path in transcript_paths(manifest, lane):
        if not path.exists():
            continue
        try:
            result = probe([lsof, '-nP', '-t', '--', str(path)])
        except (OSError, subprocess.SubprocessError) as exc:
            raise ToolError('lsof', 6, 'lsof_probe_failed') from exc
        pids = result.stdout.split()
        if (result.returncode not in (0, 1) or result.stderr.strip()
                or any(not pid.isdecimal() for pid in pids)):
            raise ToolError('lsof', result.returncode, 'lsof_probe_failed')
        if pids:
            holders.update(map(int, pids))
    return holders
