"""Small durable-state primitives shared by command implementations."""
import json
import os
import stat
import uuid
from urllib.parse import quote


def name_key(value):
    return quote(value, safe='')


def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def _replace_json(path, value, check=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = 0o600
    try:
        # Create privately even under a permissive umask. Preserve an existing
        # target's mode only after the replacement contents have been written.
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
            stream.write('\n')
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        if check is not None:
            check()
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path, value):
    _replace_json(path, value)


class _Changed(Exception):
    pass


def _snapshot(path):
    import hashlib
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        data = None
    return data, hashlib.sha256(data).digest() if data is not None else None


def update_json(path, transform, *, before_replace=None, after_replace=None):
    """Optimistic settings transaction, with one re-merge and a shared sibling lock.

    Compute from a byte-hashed snapshot. Under the exclusive sibling lock, stage
    the replacement, persist any receipt, then compare the current hash directly
    before rename. A changed document is re-read and transformed once; a second
    change aborts. Receipt finalization also runs before releasing the lock.
    """
    import fcntl
    import copy
    from .errors import ManifestError
    def read():
        data, digest = _snapshot(path)
        return (json.loads(data) if data is not None else {}), digest
    current, digest = read()
    updated = transform(copy.deepcopy(current))
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + '.lock')
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        for attempt in range(2):
            def check():
                # Do not prepare stale receipts if another lock holder committed
                # while this writer was computing or waiting for the lock.
                if _snapshot(path)[1] != digest:
                    raise _Changed()
                if before_replace is not None:
                    before_replace(current, updated)
                if _snapshot(path)[1] != digest:
                    raise _Changed()
            try:
                _replace_json(path, updated, check)
            except _Changed:
                if attempt:
                    raise ManifestError('settings_update_conflict: changed twice before replacement: ' + str(path)) from None
                current, digest = read()
                updated = transform(copy.deepcopy(current))
                continue
            if after_replace is not None:
                after_replace(current, updated)
            return updated
