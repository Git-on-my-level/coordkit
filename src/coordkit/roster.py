"""Bounded roster-drift findings for `doctor`.

The bus is append-only and can be much larger than one doctor run should read.
This module inspects a bounded tail through the shared physical-line reader and
reports only counts plus a few sample names. Findings are warnings: roster drift
is an operational signal, not a reason to fail `up --apply`.
"""
import time

from . import addressed, bus, routing
from .manifest import duration
from .process import parse_ts
from .state import read_json
from .usage import tail_lines

WINDOW = 24 * 3600
TAIL_BYTES = 8 * 1024 * 1024
SAMPLE = 5


def _rows(manifest):
    """(rows, truncated) from a bounded complete-line tail of the bus log."""
    path = manifest.paths.bus_log
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return [], False
    lines = tail_lines(path, TAIL_BYTES)
    rows = [row for _, row in bus.parse_lines(lines)]
    return rows, size > TAIL_BYTES


def _stamp(row):
    try:
        return parse_ts(row['ts']).timestamp()
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _declared_names(manifest):
    """Names the manifest or the incumbent `readers.json` already declares."""
    names = set(manifest.addressed.agents) | set(manifest.addressed.roles)
    for spec in manifest.addressed.roles.values():
        if isinstance(spec, dict):
            names.update(spec.get('members') or [])
        elif isinstance(spec, list):
            names.update(spec)
    names.update(agent.label for agent in manifest.agents)
    names.update(lane.name for lane in manifest.lanes)
    names.update(manifest.mcp.post_as)
    parent = manifest.paths.bus_log.parent
    for path in (parent / 'readers.json', parent / 'state' / 'readers.json',
                 manifest.paths.root / 'readers.json'):
        try:
            doc = read_json(path, {})
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        readers = doc.get('readers', doc)
        if isinstance(readers, dict):
            names.update(name for name in readers if isinstance(name, str) and name)
    return names


def health(manifest, at=None):
    """One doctor line: counts and at most five sample names per finding."""
    at = time.time() if at is None else at
    try:
        rows, truncated = _rows(manifest)
        stamped = [(row, stamp) for row in rows if (stamp := _stamp(row)) is not None]
        # The first row is the append-order byte boundary of a truncated tail.
        # A later out-of-order (relayed) timestamp says nothing about rows before it.
        boundary = _stamp(rows[0]) if rows else None

        def covered(window):
            # A full read covers all history. A truncated tail proves a window only
            # when its boundary row is at or before the window start.
            return not truncated or (boundary is not None and boundary <= at - window)

        agents = set(manifest.addressed.agents)
        aliases = bus.aliases(manifest)
        alias_names = set(aliases) | set(aliases.values())
        declared = _declared_names(manifest)

        last_post = {}
        sent = {}
        received = {}
        for row, stamp in stamped:
            if stamp > at:
                continue
            sender = row.get('frm')
            if isinstance(sender, str) and sender:
                if sender not in last_post or stamp > last_post[sender]:
                    last_post[sender] = stamp
                if at - WINDOW <= stamp:
                    sent[sender] = sent.get(sender, 0) + 1
            recipient = row.get('to')
            if isinstance(recipient, str) and at - WINDOW <= stamp:
                for name in {part.strip() for part in recipient.split(',')}:
                    if name:
                        received[name] = received.get(name, 0) + 1

        silent_after = duration(manifest.addressed.roster_silent_after)
        silent = []
        if covered(silent_after):
            for name in sorted(agents):
                last = last_post.get(name)
                if last is not None and last > at - silent_after:
                    continue
                try:
                    live, _ = addressed.listening(manifest, name, at=at, rows=rows)
                except (OSError, ValueError, KeyError, TypeError):
                    live = False
                if not live:
                    silent.append(name)

        unenrolled = sorted(
            name for name, count in sent.items()
            if name not in agents and name not in alias_names and name not in declared
            and not routing.internal_name(manifest, name)
            and count > manifest.addressed.roster_unenrolled_posts)

        sinks = []
        if covered(WINDOW):
            sinks = sorted(
                name for name in agents
                if sent.get(name, 0) == 0
                and received.get(name, 0) > manifest.addressed.roster_sink_received)

        detail = (f'enrolled={len(agents)} silent={len(silent)} '
                  f'unenrolled={len(unenrolled)} sink={len(sinks)}')
        if truncated:
            detail += ' truncated=true'
        if not (silent or unenrolled or sinks):
            return detail
        parts = ['WARN', detail]
        for label, names in (('silent', silent), ('unenrolled', unenrolled), ('sink', sinks)):
            if names:
                parts.append(f'{label}_sample=' + ','.join(names[:SAMPLE]))
        return ' '.join(parts)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Roster drift is advisory; an unreadable optional source must not turn
        # doctor red or block an otherwise valid apply.
        return f'WARN roster_unavailable {type(exc).__name__}: {exc}'
