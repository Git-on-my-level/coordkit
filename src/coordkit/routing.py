"""Read-only roster routing and advisory locks shared by hook delivery."""
import contextlib
import fcntl
from .state import read_json


def cfg(manifest):
    return manifest.addressed


def home(manifest):
    return manifest.paths.root/'state'/'addressed'


def registry(manifest):
    """Manifest agents plus existing runtime registrations (read-only compatibility)."""
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
