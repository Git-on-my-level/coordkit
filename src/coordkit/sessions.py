"""Live Claude Code sessions across every config profile, keyed by profile and PID.

Claude Code writes ``<config>/sessions/<pid>.json`` for each running session. A
session's display name is often derived (``omi-75``) and is not unique across
profiles, so a lane is never resolved by that name alone: it is matched by its
tmux session or an explicit (non-derived) name, and an ambiguous match is refused.
"""
import json
import os
from pathlib import Path

FIELDS = ('profile', 'pid', 'session_id', 'name', 'name_source', 'tmux', 'status', 'kind', 'cwd', 'socket')


def roots(home=None, extra=()):
    home = Path(home or Path.home())
    found = sorted(p for p in home.glob('.claude*') if p.is_dir() and (p/'sessions').is_dir())
    for path in extra:
        path = Path(path).expanduser()
        if path.is_dir() and (path/'sessions').is_dir() and path not in found:
            found.append(path)
    return found


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def discover(home=None, extra=(), is_alive=alive):
    rows = []
    config = os.environ.get('CLAUDE_CONFIG_DIR')
    for root in roots(home, tuple(extra) + ((config,) if config else ())):
        for path in sorted((root/'sessions').glob('*.json')):
            try:
                record = json.loads(path.read_text())
            except (OSError, ValueError, UnicodeError):
                continue
            pid = record.get('pid')
            if not isinstance(record, dict) or type(pid) is not int or not is_alive(pid):
                continue
            rows.append({
                'profile': root.name, 'pid': pid, 'session_id': record.get('sessionId'),
                'name': record.get('name'), 'name_source': record.get('nameSource'),
                'tmux': record.get('tmux'), 'status': record.get('status'), 'kind': record.get('kind'),
                'cwd': record.get('cwd'), 'socket': record.get('messagingSocketPath'),
            })
    return rows


def key(row):
    return f"{row['profile']}:{row['pid']}"


def tmux_session(row):
    return (row.get('tmux') or '').split(':', 1)[0] or None


def collisions(rows):
    seen = {}
    for row in rows:
        if row.get('name'):
            seen.setdefault(row['name'], []).append(key(row))
    return {name: keys for name, keys in seen.items() if len(keys) > 1}


def resolve(rows, lane):
    """Exactly one session for a lane, or a refusal naming why."""
    if ':' in lane and lane.split(':', 1)[1].isdecimal():
        matches = [r for r in rows if key(r) == lane]
    else:
        matches = [r for r in rows if tmux_session(r) == lane]
        if not matches:
            matches = [r for r in rows if r.get('name') == lane and r.get('name_source') != 'derived']
    if not matches:
        return None, f'no_session: {lane}'
    if len(matches) > 1:
        return None, f'ambiguous_session: {lane} -> ' + ', '.join(key(r) for r in matches)
    return matches[0], None


def main(argv):
    from .ops import parser_for
    options = parser_for('sessions').parse_args(argv)
    rows = discover()
    if options.lane:
        row, error = resolve(rows, options.lane)
        if error:
            print(error)
            return 2
        rows = [row]
    if options.json:
        print(json.dumps({'sessions': rows, 'collisions': collisions(rows)}, indent=2))
        return 0
    for row in rows:
        print(f"{key(row):<20} {row.get('status') or '-':<7} {tmux_session(row) or '-':<22} "
              f"{row.get('name') or '-'}{'*' if row.get('name_source') == 'derived' else ''}  {row.get('session_id') or '-'}")
    for name, keys in collisions(rows).items():
        print(f'COLLISION name={name}: ' + ', '.join(keys))
    return 0
