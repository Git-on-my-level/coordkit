"""Verified, offline-testable managed releases; ordinary venv installs stay notify-only."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import sys
import tempfile
import time
import urllib.request
import zipfile
from . import __version__
from .manifest import ManifestError, load
from .process import run
from .state import read_json, write_json

REPOSITORY = 'Git-on-my-level/coordkit'
VERSION = re.compile(r'^v?(\d+)\.(\d+)\.(\d+)$')
MAX_DOWNLOAD = 64 * 1024 * 1024


def home():
    return Path(os.environ.get('COORDKIT_INSTALL_HOME', '~/.local/share/coordkit')).expanduser()


def state_dir():
    return Path(os.environ.get('COORDKIT_STATE_HOME', '~/.local/state/coordkit')).expanduser()


@contextlib.contextmanager
def locked():
    import fcntl
    path = state_dir()/'update.lock'; path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def register(manifest, extra_jobs=()):
    manifest.writable()
    with locked():
        path = state_dir()/'manifests.json'
        docs = read_json(path, {})
        old = docs.get(str(manifest.file), {})
        docs[str(manifest.file)] = dict(manifest=str(manifest.file), label_prefix=manifest.launchd.label_prefix,
                                        extra_jobs=list(extra_jobs) or old.get('extra_jobs', []))
        write_json(path, docs)


def health():
    current = home()/'current'
    if not current.is_symlink():
        return 'unmanaged install; notify-only'
    try:
        target = current.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ManifestError('selfupdate_invalid_current: ' + str(exc)) from exc
    receipt = read_json(target/'install-manifest.json')
    if target.parent != home()/'releases' or not receipt:
        raise ManifestError('selfupdate_invalid_current')
    if not Path(sys.executable).is_file():
        raise ManifestError('selfupdate_python_missing')
    # A source checkout can inspect a managed install without impersonating it.
    if target in Path(__file__).resolve().parents and receipt['version'].removeprefix('v') != __version__:
        raise ManifestError('selfupdate_version_mismatch')
    return receipt['version']


def fetch(url, limit=MAX_DOWNLOAD):
    if not url.startswith('https://'):
        raise ManifestError('update_requires_https')
    headers = {'Accept':'application/vnd.github+json', 'User-Agent':'coordkit/' + __version__}
    token = os.environ.get('GITHUB_TOKEN')
    # Never send credentials to release asset/CDN hosts.
    if token and url.startswith('https://api.github.com/'):
        headers['Authorization'] = 'Bearer ' + token
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=10) as response:
        body = response.read(limit + 1)
    if len(body) > limit:
        raise ManifestError('update_download_too_large')
    return body


def latest():
    repository = os.environ.get('COORDKIT_RELEASE_REPO', REPOSITORY)
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', repository):
        raise ManifestError('invalid_release_repository')
    release = json.loads(fetch(f'https://api.github.com/repos/{repository}/releases/latest', 1024*1024))
    tag = release.get('tag_name', '')
    if not VERSION.fullmatch(tag) or release.get('prerelease') or release.get('draft'):
        raise ManifestError('update_invalid_release_version')
    return release


def check():
    state = read_json(state_dir()/'update-state.json', {})
    policy = state_dir()/'update-policy.json'
    if not policy.exists():
        write_json(policy, {'schema_version':1, 'mode':'notify'})
    try:
        release = latest()
        state.update(installed_version=__version__, latest_version=release['tag_name'],
                     release_url=release.get('html_url', ''), checked_on=time.time(), last_error=None)
        write_json(state_dir()/'update-state.json', state)
        return release
    except Exception as exc:
        state.update(last_error=type(exc).__name__, last_error_at=time.time())
        write_json(state_dir()/'update-state.json', state)
        raise


def unpack(wheel, destination, version):
    """A pure-Python wheel is an import tree; no pip or arbitrary install scripts."""
    from io import BytesIO
    total = 0
    with zipfile.ZipFile(BytesIO(wheel)) as archive:
        names = set()
        for entry in archive.infolist():
            path = PurePosixPath(entry.filename)
            mode = entry.external_attr >> 16
            if path.is_absolute() or '..' in path.parts or '\\' in entry.filename or stat.S_ISLNK(mode):
                raise ManifestError('update_unsafe_wheel_path')
            total += entry.file_size
            if total > MAX_DOWNLOAD or entry.filename in names:
                raise ManifestError('update_invalid_wheel_size_or_duplicate')
            names.add(entry.filename)
        metadata = [n for n in names if n.endswith('.dist-info/METADATA')]
        wheelinfo = [n for n in names if n.endswith('.dist-info/WHEEL')]
        if len(metadata) != 1 or len(wheelinfo) != 1:
            raise ManifestError('update_invalid_wheel_metadata')
        text = archive.read(metadata[0]).decode()
        if not re.search(r'^Name: coordkit\s*$', text, re.M | re.I) or not re.search(r'^Version: ' + re.escape(version.removeprefix('v')) + r'\s*$', text, re.M):
            raise ManifestError('update_wheel_version_mismatch')
        if 'Root-Is-Purelib: true' not in archive.read(wheelinfo[0]).decode():
            raise ManifestError('update_requires_pure_python_wheel')
        if any(re.match(r'Requires-Dist:', line) for line in text.splitlines()):
            raise ManifestError('update_wheel_dependencies_not_supported')
        archive.extractall(destination)
    if not (destination/'coordkit/cli.py').is_file():
        raise ManifestError('update_wheel_missing_package')


def smoke(path, version):
    script = ('import sys; sys.path.insert(0, sys.argv[1]); from coordkit import __version__; '
              'assert __version__ == sys.argv[2], (__version__, sys.argv[2]); '
              'from coordkit.cli import main; main(["--version"])')
    result = run([sys.executable, '-I', '-c', script, str(path), version.removeprefix('v')], timeout=30)
    if result.returncode:
        raise ManifestError('update_smoke_failed: ' + result.stderr.strip())


def swap(target):
    directory = home(); directory.mkdir(parents=True, exist_ok=True)
    current = directory/'current'
    if current.exists() and not current.is_symlink():
        raise ManifestError('update_current_not_symlink')
    temporary = directory/('.current-' + secrets.token_hex(8))
    temporary.symlink_to(target, target_is_directory=True)
    try:
        os.replace(temporary, current)
    finally:
        temporary.unlink(missing_ok=True)


def shim():
    # The shared shim is created only on explicit adoption. Updates do not replace
    # a user-owned executable (including a uv/pipx entry point).
    bindir = Path(os.environ.get('COORDKIT_BIN_HOME', '~/.local/bin')).expanduser()
    bindir.mkdir(parents=True, exist_ok=True)
    target = bindir/'coordkit'
    import shlex
    text = '#!/bin/sh\n# coordkit-managed\nexec ' + shlex.quote(str(home()/'current/bin/coordkit')) + ' "$@"\n'
    if target.exists() and target.read_text() != text:
        raise ManifestError('update_shim_conflict: existing command is not coordkit-managed')
    target.write_text(text); target.chmod(0o755)


def reconcile(manifest=None, services=True):
    from . import hooks
    from .paths import python_env
    records = read_json(state_dir()/'manifests.json', {})
    if manifest:
        records.setdefault(str(manifest.file), {'manifest':str(manifest.file), 'extra_jobs':[]})
    changes = []
    for record in records.values():
        file = Path(record['manifest'])
        if not file.is_file():
            raise ManifestError('registered_manifest_missing: ' + str(file))
        project = load(file)
        project.writable()
        # Stable path is explicit even when update runs from a source checkout.
        previous = os.environ.get('COORDKIT_HOME')
        if (home()/'current').is_dir():
            os.environ['COORDKIT_HOME'] = str(home()/'current')
        try:
            changes.extend(hooks.reconcile(project))
            if services:
                from .services import manager
                from .lifecycle import templates
                state = read_json(project.paths.root/'lifecycle.json', {})
                owned = {r['label'] for r in state.get('jobs', [])}
                if (project.paths.root/'mcp-service.json').exists():
                    owned.add(project.launchd.label_prefix+'.mcp')
                if (project.paths.root/'relay/service.json').exists():
                    owned.add(project.launchd.label_prefix+'.relay')
                specs = templates(project) if owned else []
                service = manager(project)
                for spec in specs:
                    if spec['Label'] not in owned:
                        continue
                    path = service.path(spec['Label'])
                    rendered = service.render(spec)
                    differs = path.read_bytes() != rendered if service.name == 'launchd' and path.exists() else True
                    if service.name == 'systemd':
                        differs = bool(service.drift(spec))
                    if differs:
                        service.install(spec, previous=True)
                        changes.append(spec['Label'])
                for label in record.get('extra_jobs', []):
                    service.kick(label)
        finally:
            if previous is None:
                os.environ.pop('COORDKIT_HOME', None)
            else:
                os.environ['COORDKIT_HOME'] = previous
        if hooks.drift(project) or hooks.question_drift(project, include_pending=False):
            raise ManifestError('update_hook_drift: ' + str(file))
    return changes


def apply(release, manifest=None, adopt=False):
    directory = home()
    if not adopt and not (directory/'current').is_symlink():
        print('UPDATE_NOTIFY use: uv tool upgrade coordkit (or pipx upgrade coordkit); then coordkit update --reconcile-hooks')
        return 0
    tag = release['tag_name']
    if not VERSION.fullmatch(tag):
        raise ManifestError('update_invalid_release_version')
    assets = {a['name']:a['browser_download_url'] for a in release['assets']}
    wheels = [n for n in assets if n.startswith('coordkit-') and n.endswith('-py3-none-any.whl')]
    if len(wheels) != 1 or 'SHA256SUMS' not in assets:
        raise ManifestError('update_release_assets_missing')
    name = wheels[0]
    checksums = dict((line.split()[-1].lstrip('*'), line.split()[0]) for line in fetch(assets['SHA256SUMS'], 1024*1024).decode().splitlines() if line.strip())
    wheel = fetch(assets[name])
    digest = hashlib.sha256(wheel).hexdigest()
    if checksums.get(name) != digest:
        raise ManifestError('update_checksum_mismatch')
    releases = directory/'releases'; releases.mkdir(parents=True, exist_ok=True)
    target = releases/tag
    if target.exists():
        receipt = read_json(target/'install-manifest.json', {})
        if receipt.get('sha256') != digest:
            raise ManifestError('update_existing_release_conflict')
        smoke(target, tag)
    else:
        with tempfile.TemporaryDirectory(prefix='.staging-', dir=releases) as staging:
            stage = Path(staging)
            unpack(wheel, stage, tag)
            smoke(stage, tag)
            import shlex
            (stage/'bin').mkdir()
            launcher = '#!/bin/sh\nexport COORDKIT_HOME=' + shlex.quote(str(directory/'current')) + '\nexport PYTHONPATH="$COORDKIT_HOME"\nexec ' + shlex.quote(sys.executable) + ' -m coordkit.cli "$@"\n'
            (stage/'bin/coordkit').write_text(launcher); (stage/'bin/coordkit').chmod(0o755)
            write_json(stage/'install-manifest.json', dict(version=tag, sha256=digest, python=sys.executable))
            os.replace(stage, target)
    current = directory/'current'
    previous = str(current.resolve()) if current.is_symlink() else None
    receipt = dict(at=time.time(), before=previous, after=str(target), version=tag)
    write_json(state_dir()/'rollback'/(str(time.time_ns()) + '.json'), receipt)
    swap(target)
    try:
        smoke(current, tag)
        reconcile(manifest)
        # First adoption must not leave a command pointing at an inactive release.
        if adopt:
            shim()
    except Exception:
        if previous:
            swap(Path(previous))
        else:
            current.unlink(missing_ok=True)
        raise
    state = read_json(state_dir()/'update-state.json', {})
    state.update(installed_version=tag, applied_at=time.time(), last_error=None)
    write_json(state_dir()/'update-state.json', state)
    print('UPDATE_OK ' + tag)
    return 0


def rollback(to=None, manifest=None):
    current = (home()/'current').resolve(strict=True)
    if to:
        if not VERSION.fullmatch(to):
            raise ManifestError('update_invalid_rollback_version')
        target = home()/'releases'/to
    else:
        records = sorted((state_dir()/'rollback').glob('*.json'), reverse=True)
        target = next((Path(r['before']) for p in records if (r := read_json(p)).get('before') and Path(r['before']) != current), None)
        if target is None:
            raise ManifestError('update_no_rollback')
    if target.parent != home()/'releases' or not target.is_dir():
        raise ManifestError('update_invalid_rollback_target')
    receipt = read_json(target/'install-manifest.json')
    smoke(target, receipt['version'])
    swap(target)
    try:
        reconcile(manifest)
    except Exception:
        swap(current)
        raise
    print('UPDATE_ROLLED_BACK ' + receipt['version'])
    return 0


def prune(keep):
    if keep < 1:
        raise ManifestError('update_keep_must_be_positive')
    releases = home()/'releases'
    current = (home()/'current').resolve()
    candidates = sorted((p for p in releases.glob('v*') if VERSION.fullmatch(p.name) and p.is_dir() and not p.is_symlink()),
                        key=lambda p: tuple(map(int, VERSION.fullmatch(p.name).groups())), reverse=True)
    protected = {current, *candidates[:keep]}
    # Preserve the last rollback anchor in addition to the requested newest N.
    for record in sorted((state_dir()/'rollback').glob('*.json'), reverse=True):
        previous = read_json(record).get('before')
        if previous and Path(previous) != current and Path(previous).is_dir():
            protected.add(Path(previous)); break
    for path in candidates:
        if path not in protected and (path/'install-manifest.json').is_file():
            shutil.rmtree(path)
            print('UPDATE_PRUNED ' + path.name)
    return 0


def main(argv, manifest=None):
    from .ops import parser_for
    parser = parser_for('update')
    args = parser.parse_args(argv)
    with locked():
        if args.rollback is not None:
            return rollback(args.rollback or None, manifest)
        if args.prune:
            return prune(args.keep)
        if args.reconcile_hooks:
            reconcile(manifest)
            print('UPDATE_RECONCILED')
            return 0
        release = check()
        print('UPDATE_AVAILABLE ' + release['tag_name'])
        return apply(release, manifest, args.adopt) if args.apply or args.adopt else 0
