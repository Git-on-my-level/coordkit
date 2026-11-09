"""Portable, committed handoff authority and consumer-side pickup checks."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

from .errors import ManifestError

REPO_REF = re.compile(r'repo:([A-Za-z0-9][A-Za-z0-9_.-]*):(.+)@sha256:([0-9a-f]{64})')


def relative(value):
    path = PurePosixPath(value)
    return bool(value and not path.is_absolute() and '..' not in path.parts
                and '\\' not in value and str(path) == value and value != '.')


def validate_config(config):
    if config.max_bytes < 1 or not config.directories or any(not relative(p) for p in config.directories):
        raise ManifestError('invalid_value: handoffs needs relative directories and positive max_bytes')
    if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name) for name in config.repos):
        raise ManifestError('invalid_value: handoffs.repos names must be plain names')
    if config.required and not config.repos:
        raise ManifestError('invalid_value: handoffs.required needs handoffs.repos')


def stored_ref(value):
    return value if value.startswith('repo:') else str(Path(value).expanduser().resolve())


def repo_file(manifest, value, writer=None):
    match = REPO_REF.fullmatch(value)
    if not match:
        raise ValueError('expected repo:NAME:PATH@sha256:<64 lowercase hex digits>')
    name, path, digest = match.groups()
    config = manifest.handoffs
    if name not in config.repos or not relative(path):
        raise ValueError('unknown repository or non-relative handoff path')
    if not any(PurePosixPath(directory) in PurePosixPath(path).parents for directory in config.directories):
        raise ValueError('handoff path outside configured tracked directories')
    if writer is not None and not PurePosixPath(path).name.startswith(writer + '--'):
        raise ValueError('handoff filename must start with poster identity followed by --')
    root = manifest.path(config.repos[name])
    candidate = root / path
    try:
        if not candidate.resolve(strict=True).is_relative_to(root):
            raise ValueError('handoff path escapes repository root')
        # Reject symlinks even within the root: Git records the link, not its target's bytes.
        if any(p.is_symlink() for p in (candidate, *candidate.parents) if p != root and p.is_relative_to(root)):
            raise ValueError('handoff path must not contain symlinks')
        if not candidate.is_file():
            raise ValueError('handoff must be a regular file')
        with candidate.open('rb') as stream:
            data = stream.read(config.max_bytes + 1)
        if len(data) > config.max_bytes:
            raise ValueError('handoff exceeds max_bytes; use a transport branch and commit SHA')
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('handoff sha256 mismatch')
        result = subprocess.run(['git', '-C', str(root), 'rev-parse', '--verify', 'HEAD:' + path],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
        blob = subprocess.run(['git', '-C', str(root), 'hash-object', '--stdin'], input=data,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
        if result.returncode or blob.returncode or result.stdout != blob.stdout:
            raise ValueError('handoff must be committed at HEAD with the named digest')
        return candidate
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        raise ValueError('handoff unavailable on this host') from None


def path_like(value):
    if re.match(r'[A-Za-z][A-Za-z0-9+.-]*:', value):
        return False
    return value.startswith(('/', './', '../', '~')) or '/' in value


def validate_post(manifest, options):
    """Explicit degradation is durable metadata, never inferred from --force."""
    reason = getattr(options, 'degrade_handoff', None)
    if reason is not None and not reason.strip():
        raise ValueError('--degrade-handoff needs a visible reason')
    refs = [value for value in (options.ref, options.evidence) if value]
    roots = [manifest.path(p) for p in manifest.handoffs.repos.values()] or [manifest.file.parent]
    portable = False
    for value in refs:
        if value.startswith('repo:'):
            # A degrade flag cannot excuse a corrupt portable authority.
            repo_file(manifest, value, options.frm)
            portable = True
        elif path_like(value) or value == options.ref:
            path = Path(value).expanduser().resolve()
            if not any(path.is_relative_to(root) for root in roots) and not reason:
                raise ValueError('path outside configured repository roots; use --degrade-handoff REASON explicitly')
            if options.kind == 'handoff' and manifest.handoffs.required and not reason:
                raise ValueError('handoff paths need repo-relative authority and sha256')
    if options.kind == 'handoff' and manifest.handoffs.required and not portable and not reason:
        raise ValueError('handoff needs --ref or --evidence repo:NAME:PATH@sha256:DIGEST')
    if reason:
        print(f'WARNING: DEGRADED HANDOFF: {reason}', file=sys.stderr)
    return reason


def local_digest(manifest, value):
    path = Path(value).expanduser()
    if not path.is_file():
        raise ValueError('handoff must be a readable regular file')
    with path.open('rb') as stream:
        data = stream.read(manifest.handoffs.max_bytes + 1)
    if len(data) > manifest.handoffs.max_bytes:
        raise ValueError('handoff exceeds max_bytes; use a transport branch and commit SHA')
    return hashlib.sha256(data).hexdigest()


def local_digests(manifest, options):
    """Bind explicitly degraded local payloads too; never print their contents."""
    result = {}
    for value in (options.ref, options.evidence):
        if value and not value.startswith('repo:') and (path_like(value) or value == options.ref):
            try:
                result[stored_ref(value)] = local_digest(manifest, value)
            except OSError:
                raise ValueError('degraded handoff path unavailable on posting host') from None
    return result


def portable(work):
    return bool(work.get('handoff_degraded') or work.get('handoff_bounce')
                or any(str(work.get(k, '')).startswith('repo:') for k in ('ref', 'evidence')))


def check_pickup(manifest, work, reader, rows):
    """Fail a claim immediately and return the repair obligation to its poster."""
    if work.get('kind') != 'handoff':
        return True
    from . import bus
    from .addressed import assignees
    current = work
    for reply in rows:
        allowed = {bus.resolve(n) for n in assignees(current, manifest)}
        if (reply.get('re') == work['id'] and reply.get('kind') == 'ack' and reply.get('owner')
                and not (reply.get('copy_of') or reply.get('escalation'))
                and (not portable(work) or reply.get('handoff_verified') == work['id'])
                and bus.resolve(reply.get('frm')) in allowed and reply['owner'] != reply['frm']):
            current = dict(current, owner=reply['owner'])
    if bus.resolve(reader) not in {bus.resolve(n) for n in assignees(current, manifest)}:
        raise ManifestError('handoff pickup requires the assigned reader')
    if work.get('handoff_bounce'):
        return True
    try:
        # Automatic long-text spills are local display detail, not handoff authority.
        ref = work.get('ref') if work.get('ref') != work.get('spill_ref') else None
        refs = [v for v in (ref, work.get('evidence')) if v]
        for value in refs:
            if value.startswith('repo:'):
                repo_file(manifest, value, work['frm'])
            elif path_like(value):
                # Legacy/degraded rows still have to be readable by the consumer.
                digest = local_digest(manifest, value)
                expected = work.get('handoff_local_digests', {}).get(value)
                if expected and expected != digest:
                    raise ValueError('degraded handoff sha256 mismatch')
        if manifest.handoffs.required and not work.get('handoff_degraded') and not any(v.startswith('repo:') for v in refs):
            raise ValueError('handoff has no repo-relative digest authority')
        if manifest.handoffs.required and work.get('handoff_degraded') and not work.get('handoff_local_digests'):
            raise ValueError('degraded handoff has no readable digest-bound payload')
        return True
    except (ValueError, OSError) as exc:
        bounce_id = 'handoff-bounce-' + hashlib.sha256((work['id'] + ':' + reader).encode()).hexdigest()[:16]
        def absent(stream):
            # Check under the append flock too, even if a caller supplied stale rows.
            stream.seek(0)
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get('id') == bounce_id:
                    return False
            return True
        if not any(r.get('id') == bounce_id for r in rows):
            bus.append([dict(id=bounce_id, ts=bus.now().isoformat(timespec='seconds'), origin='coordkit',
                             frm=reader, to=work['frm'], owner=work['frm'], kind='handoff',
                             topic=work.get('topic', 'general'), re=work['id'], handoff_bounce=str(exc),
                             text=f"Handoff {work['id']} unreadable: {exc}. Repair authority and repost."[:manifest.bus.text_cap])],
                       project=False, before_append=absent)
        print(f"refused: handoff {work['id']} bounced to {work['frm']}: {exc}", file=sys.stderr)
        return False
