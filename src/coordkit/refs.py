"""Shared structured-ref authorization for local and connector readers."""
from pathlib import Path


def ref_file(manifest, row, full=False):
    """Trust only manifest-derived roots; resolve the candidate to reject traversal and symlink escapes.

    Only structured refs written by the bus grant access. Text markers are untrusted
    caller input, even when they look like the bus's spill notice."""
    ref = row.get('ref')
    if row.get('spill_ref') and (full or not ref):
        ref = row['spill_ref']
    if not isinstance(ref, str) or not ref:
        raise ValueError('row has no ref file')
    if ref.startswith('repo:'):
        from .handoffs import repo_file
        return repo_file(manifest, ref, row.get('frm'), fetch=True)
    swarm = manifest.paths.bus_log.parent.resolve()
    candidate = Path(ref)
    if not candidate.is_absolute():
        candidate = swarm / candidate
    try:
        candidate = candidate.resolve(strict=True)
        # Read existing archives only; this accessor introduces no state marker or writer.
        roots = [manifest.path(p) for p in manifest.mcp.ref_roots]
        allowed = any(parent.match(str(root)) for root in roots for parent in candidate.parents)
        if not allowed:
            raise ValueError('ref outside allowed manifest ref roots')
        if not candidate.is_file():
            raise ValueError('ref is not a regular file')
        return candidate
    except (OSError, RuntimeError):
        raise ValueError('ref unavailable') from None
