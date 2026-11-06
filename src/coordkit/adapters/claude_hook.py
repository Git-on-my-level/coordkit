"""Bounded hook configuration projection, independent of orchestration schemas.

Delivery only needs paths and the roster. Keep this projection small so every
Claude tool call does not build the full typed service/orchestration manifest.
The main CLI still validates the complete manifest before managing any hooks.
"""
from pathlib import Path
import re
import tomllib
from types import SimpleNamespace
from ..errors import ManifestError


def load(path=None):
    if path is None:
        for parent in (Path.cwd(), *Path.cwd().parents):
            if (parent/'coordkit.toml').is_file():
                path = parent/'coordkit.toml'; break
    if path is None:
        raise ManifestError('manifest_not_found')
    file = Path(path).expanduser().resolve()
    with file.open('rb') as stream:
        raw = tomllib.load(stream)
    project = raw.get('project', {})
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', project.get('name', '')) or not re.fullmatch(r'[\w.-]+/[\w.-]+', project.get('git_repo', '')):
        raise ManifestError('invalid_hook_project')
    paths = raw.get('paths', {})
    modes = [raw[k]['mode'] for k in ('paths', 'rc_host') if isinstance(raw.get(k), dict) and 'mode' in raw[k]]
    if 'mode' in raw:
        modes.append(raw['mode'])
    if any(mode != 'live' for mode in modes):
        raise ManifestError('hook_not_live')
    def resolve(value):
        if not isinstance(value, str) or not value:
            raise ManifestError('invalid_hook_path')
        return (file.parent/Path(value).expanduser()).resolve()
    root = resolve(paths.get('root', '.coordkit'))
    bus = resolve(paths['bus_log']) if 'bus_log' in paths else root/'bus.jsonl'
    addressed = raw.get('addressed', {})
    agents, roles = addressed.get('agents', {}), addressed.get('roles', {})
    if not isinstance(agents, dict) or not isinstance(roles, dict):
        raise ManifestError('invalid_hook_roster')
    return SimpleNamespace(file=file, mode='live', paths=SimpleNamespace(root=root, bus_log=bus),
                           addressed=SimpleNamespace(agents=agents, roles=roles))
