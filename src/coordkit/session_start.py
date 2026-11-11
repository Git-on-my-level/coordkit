"""Private, session-scoped caller declaration for unmanaged Claude commands."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import unicodedata

from .hook_runtime import hook_deadline

TIMEOUT = .25
MAX_INPUT = 32 * 1024
MAX_BINDINGS = 256 * 1024
MAX_ENV = 64 * 1024
MAX_APPEND = 2048
MANAGED = ('AGENTCTL_EXECUTION_ID', 'AGENTCTL_ADAPTER', 'AGENTCTL_AUTHORITY')


def valid_session(session):
    return (isinstance(session, str) and bool(session.strip())
            and len(session.encode('utf-8')) <= 256
            and not any(unicodedata.category(c) == 'Cc' for c in session))


def scoped_lane(manifest, session):
    """Use explicit pins/bindings only; never call the transcript resolver."""
    from .identity import home
    lanes = [lane for lane in manifest.lanes if getattr(lane, 'harness', '') in ('', 'claude')]
    names = {lane.name for lane in lanes if lane.session_id == session}
    try:
        with (home(manifest)/'sessions.json').open('rb') as stream:
            raw = stream.read(MAX_BINDINGS + 1)
        if len(raw) > MAX_BINDINGS:
            return None
        bindings = json.loads(raw.decode('utf-8'))
    except FileNotFoundError:
        bindings = {}
    if not isinstance(bindings, dict):
        return None
    if session in bindings:
        binding = bindings[session]
        lane = next((lane for lane in lanes if lane.name == binding['name']), None)
        if lane is None or lane.session_id not in ('', session):
            return None
        names.add(lane.name)
    if len(names) != 1:
        return None
    return next(lane for lane in lanes if lane.name in names)


def private_append(config_dir, session, target, block):
    """Append once through checked descriptors; never follow a path symlink.

    Claude 2.1.322 supplies config/session-env/session/file. Unknown layouts
    fail open rather than authorizing writes elsewhere in the config directory.
    The native hook supplies a not-yet-created file, which we create as 0600.
    """
    root, path = Path(config_dir), Path(target)
    if not root.is_absolute() or not path.is_absolute() or len(target) > 4096:
        return
    if '..' in root.parts or '..' in path.parts or len(root.parts) > 64:
        return
    parts = path.relative_to(root).parts
    if len(parts) != 3 or parts[:2] != ('session-env', session):
        return
    uid = os.getuid()
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    with contextlib.ExitStack() as stack:
        def directory(name, parent=None, owned=True):
            fd = os.open(name, directory_flags, dir_fd=parent)
            stack.callback(os.close, fd)
            info = os.fstat(fd)
            if owned and (info.st_uid != uid or stat.S_IMODE(info.st_mode) & 0o022):
                raise ValueError('untrusted session directory')
            return fd
        # Check every component, including ancestors of the configured root.
        parent = directory('/', owned=False)
        for part in root.parts[1:]:
            parent = directory(part, parent, owned=False)
        info = os.fstat(parent)
        if info.st_uid != uid or stat.S_IMODE(info.st_mode) & 0o022:
            return
        for part in parts[:-1]:
            parent = directory(part, parent)
        flags = os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        try:
            fd = os.open(parts[-1], flags, dir_fd=parent)
        except FileNotFoundError:
            fd = os.open(parts[-1], flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
        stack.callback(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > MAX_ENV):
            return
        raw = os.read(fd, MAX_ENV + 1)
        if len(raw) != info.st_size or block in raw or len(raw) + len(block) > MAX_ENV:
            return
        current = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        if ((current.st_dev, current.st_ino, current.st_size, current.st_mode, current.st_nlink, current.st_uid)
                != (info.st_dev, info.st_ino, info.st_size, info.st_mode, info.st_nlink, info.st_uid)):
            return
        written = os.write(fd, block)  # one bounded append under the nonblocking lock
        if written != len(block) and os.fstat(fd).st_size == info.st_size + written:
            os.ftruncate(fd, info.st_size)


def bind(manifest, payload):
    if (payload.get('hook_event_name') != 'SessionStart'
            or any(key in os.environ for key in MANAGED)):
        return
    session = payload.get('session_id')
    if not valid_session(session):
        return
    lane = scoped_lane(manifest, session)
    if lane is None:
        return
    from .deliver import lane_setting
    if not lane_setting(manifest, lane.name, 'stop_requires_agentctl_collection'):
        return
    target = os.environ.get('CLAUDE_ENV_FILE')
    if not target:
        return
    # Recheck when Bash sources this file too: a declaration persisted by an
    # unmanaged lifecycle must not override managed context introduced later.
    block = ('\n# coordkit caller binding v1\n'
             'if [ -z "${AGENTCTL_EXECUTION_ID+x}${AGENTCTL_ADAPTER+x}${AGENTCTL_AUTHORITY+x}" ]; then\n'
             'export AGENTCTL_CALLER_PROVIDER=claude\n'
             'export AGENTCTL_CALLER_NATIVE_SESSION_ID=' + shlex.quote(session) + '\nfi\n').encode('utf-8')
    if len(block) <= MAX_APPEND:
        private_append(lane.config_dir, session, target, block)


def run(manifest, stdin=None):
    """All failures are silent; this hook never emits a decision or context."""
    try:
        with hook_deadline(TIMEOUT):
            stream = getattr(sys.stdin, 'buffer', sys.stdin)
            raw = stream.read(MAX_INPUT + 1) if stdin is None else stdin
            raw = raw.encode('utf-8') if isinstance(raw, str) else raw
            if len(raw) <= MAX_INPUT:
                bind(manifest, json.loads(raw.decode('utf-8')))
    except BaseException:
        pass
    return 0
