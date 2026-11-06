"""Explicit session identity shared by delivery and question adapters."""
import os
import re
from .state import read_json

BUS_NAME = re.compile(r'bus name is\W{0,4}([A-Za-z0-9][A-Za-z0-9_.-]{1,63})', re.I)


def home(manifest):
    return manifest.paths.root/'state'/'questions'


def asker_for(manifest, session_id, transcript_path=None, env=None, *, transcript_max_bytes=None):
    """(bus name, how). COORDKIT_AS, then a `question bind` entry, then the transcript's 'bus name is X'.
    Unknown sessions get an explicit unknown-<session> name routed to the relay; never a guess."""
    env = os.environ if env is None else env
    if env.get('COORDKIT_AS'):
        return env['COORDKIT_AS'], 'env'
    bound = (read_json(home(manifest)/'sessions.json', {}) or {}).get(session_id or '')
    if bound:
        return bound['name'], 'bound:' + (bound.get('remote') or '')
    if transcript_path:
        try:
            with open(transcript_path, 'rb') as stream:
                # Hooks use a bounded prefix; question capture retains its existing scan.
                lines = stream if transcript_max_bytes is None else stream.read(transcript_max_bytes).splitlines()
                for _, raw in zip(range(80), lines):
                    line = raw.decode(errors='replace')
                    if '"user"' not in line:
                        continue
                    match = BUS_NAME.search(line.replace('\\n', ' ').replace('**', '').replace('`', ''))
                    if match:
                        return match.group(1).rstrip('.'), 'transcript'
        except OSError:
            pass
    return f'unknown-{(session_id or "nosession")[:8]}', 'unknown'


