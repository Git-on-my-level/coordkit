"""Bounded Git-object reads; never inspect or update a checkout's files/index/HEAD."""
from dataclasses import dataclass
import io
import subprocess

from .process import run, ToolError


@dataclass(frozen=True)
class RepoBlob:
    ref: str
    data: bytes

    def __str__(self):
        return self.ref

    def open(self, mode='rb'):
        if mode != 'rb':
            raise ValueError('Git handoff blobs are read-only binary streams')
        return io.BytesIO(self.data)

    def read_bytes(self):
        return self.data

    def read_text(self, encoding='utf-8', errors='strict'):
        return self.data.decode(encoding, errors)


def git(root, *args):
    result = subprocess.run(['git', '--literal-pathspecs', '-C', str(root), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
    if result.returncode:
        raise ValueError('handoff Git object unavailable')
    return result.stdout


def descriptor_blob(root, path, commit, config, *, fetch=False):
    """Pinned commits read locally at post; pickup fetches even when already cached.

    The old unpinned form fetches the remote default tip in a private ref at both boundaries. A
    fetch updates objects/private refs only, never a checked-out branch or FETCH_HEAD.
    """
    default_ref = 'refs/coordkit/handoffs/' + config.remote
    if fetch or not commit:
        refspec = commit or '+HEAD:' + default_ref
        try:
            result = run(['env', 'GIT_TERMINAL_PROMPT=0', 'git', '-C', str(root),
                          '-c', 'credential.interactive=false', 'fetch', '--no-tags',
                          '--no-write-fetch-head', '--no-recurse-submodules', '--no-auto-maintenance', config.remote, refspec],
                         timeout=config.fetch_timeout_seconds)
        except ToolError:
            raise ValueError('handoff remote fetch failed or timed out') from None
        if result.returncode:
            raise ValueError('handoff remote fetch failed')
    if not commit:
        commit = git(root, 'rev-parse', '--verify', default_ref + '^{commit}').decode().strip()
    if git(root, 'cat-file', '-t', commit).strip() != b'commit':
        raise ValueError('handoff revision must name a commit object')
    entry = git(root, 'ls-tree', '-z', commit, '--', path)
    metadata, _, name = entry.rstrip(b'\0').partition(b'\t')
    parts = metadata.split()
    if name != path.encode() or len(parts) != 3 or parts[0] not in (b'100644', b'100755') or parts[1] != b'blob':
        raise ValueError('handoff tree entry must be a regular blob; symlinks are refused')
    spec = commit + ':' + path
    size = int(git(root, 'cat-file', '-s', spec).strip())
    if size > config.max_bytes:
        raise ValueError('handoff exceeds max_bytes; use a transport branch and commit SHA')
    data = git(root, 'cat-file', 'blob', spec)
    if len(data) != size:
        raise ValueError('handoff Git blob size mismatch')
    return data
