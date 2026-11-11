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
    deliver = raw.get('deliver', {})
    requires_waiter = deliver.get('stop_requires_waiter', True)
    requires_collection = deliver.get('stop_requires_agentctl_collection', True)
    fresh = deliver.get('waiter_fresh_seconds', 180)
    lanes = raw.get('lanes', [])
    if (type(requires_waiter) is not bool or type(requires_collection) is not bool or type(fresh) is not int or fresh < 0
            or any(key in lane and type(lane[key]) is not bool
                   for lane in lanes for key in ('stop_requires_waiter', 'stop_requires_agentctl_collection'))):
        raise ManifestError('invalid_hook_deliver')
    return SimpleNamespace(deliver=SimpleNamespace(stop_requires_waiter=requires_waiter,
                                                 stop_requires_agentctl_collection=requires_collection,
                                                 waiter_fresh_seconds=fresh),
                           agents=[SimpleNamespace(**a) for a in raw.get('agents', [])],
                           lanes=[SimpleNamespace(**dict(lane, config_dir=str(resolve(lane['config_dir'])),
                                                         cwd=str(resolve(lane['cwd'])), session_id=lane.get('session_id', '')))
                                  for lane in lanes],
                           mcp=SimpleNamespace(identity=raw.get('mcp', {}).get('identity') or f'ck-{project["name"]}-mcp'),
                           escalation=SimpleNamespace(internal_senders=raw.get('escalation', {}).get('internal_senders', [])),
                           questions=SimpleNamespace(config_dirs=raw.get('questions', {}).get('config_dirs', [])),
                           file=file, mode='live', paths=SimpleNamespace(root=root, bus_log=bus),
                           addressed=SimpleNamespace(agents=agents, roles=roles, rearm_grace=addressed.get('rearm_grace', '3m'),
                                                     heartbeat_every=addressed.get('heartbeat_every', '10s'),
                                                     internal_senders=addressed.get('internal_senders', ['coordkit', 'coordkit-report'])))
