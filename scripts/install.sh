#!/bin/sh
# Install a checksum-verified pure Python release into the managed symlink layout.
set -eu
COORDKIT_INSTALL_VERSION=${1:-latest}
export COORDKIT_INSTALL_VERSION
for candidate in python3.11 python3.12 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; assert sys.version_info >= (3, 11)' 2>/dev/null; then
    COORDKIT_INSTALL_PYTHON=$candidate
    break
  fi
done
: "${COORDKIT_INSTALL_PYTHON:?Python 3.11 or newer is required}"
exec "$COORDKIT_INSTALL_PYTHON" - <<'PY'
import hashlib, json, os, re, stat, sys, tempfile, urllib.request, zipfile
from io import BytesIO
from pathlib import Path, PurePosixPath
repo = os.environ.get('COORDKIT_RELEASE_REPO', 'Git-on-my-level/coordkit')
version = os.environ['COORDKIT_INSTALL_VERSION']
if not re.fullmatch(r'[\w.-]+/[\w.-]+', repo):
    raise SystemExit('invalid repository')
if version != 'latest' and not re.fullmatch(r'v\d+\.\d+\.\d+', version):
    raise SystemExit('expected latest or vX.Y.Z')
def fetch(url):
    if not url.startswith('https://'):
        raise SystemExit('HTTPS required')
    request = urllib.request.Request(url, headers={'User-Agent':'coordkit-installer'})
    with urllib.request.urlopen(request, timeout=10) as response:
        data = response.read(64*1024*1024+1)
    if len(data)>64*1024*1024:
        raise SystemExit('release too large')
    return data
suffix = 'latest' if version == 'latest' else 'tags/'+version
release = json.loads(fetch('https://api.github.com/repos/'+repo+'/releases/'+suffix))
assets = {a['name']:a['browser_download_url'] for a in release['assets']}
wheels = [n for n in assets if n.startswith('coordkit-') and n.endswith('-py3-none-any.whl')]
if len(wheels)!=1:
    raise SystemExit('expected one pure Python wheel')
name = wheels[0]
sums = {line.split()[-1].lstrip('*'):line.split()[0] for line in fetch(assets['SHA256SUMS']).decode().splitlines() if line.strip()}
wheel = fetch(assets[name])
if sums.get(name)!=hashlib.sha256(wheel).hexdigest():
    raise SystemExit('checksum mismatch')
# Only import the bootstrap updater after verification and safe extraction.
install_home = Path(os.environ.get('COORDKIT_INSTALL_HOME', '~/.local/share/coordkit')).expanduser()
install_home.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix='.bootstrap-',dir=install_home) as temporary:
    with zipfile.ZipFile(BytesIO(wheel)) as archive:
        total = 0
        for entry in archive.infolist():
            path = PurePosixPath(entry.filename)
            total += entry.file_size
            if path.is_absolute() or '..' in path.parts or '\\' in entry.filename or stat.S_ISLNK(entry.external_attr>>16) or total>64*1024*1024:
                raise SystemExit('unsafe wheel')
        archive.extractall(temporary)
    sys.path.insert(0,temporary)
    from coordkit.selfupdate import apply, locked
    with locked():
        apply(release,adopt=True)
PY
