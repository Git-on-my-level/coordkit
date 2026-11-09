"""Topic ownership from the existing bounded coordination window."""
import re

from .manifest import duration
from .process import parse_ts


APPROVES = re.compile(r'^\s*(approved?\b|go ahead\b|permission granted\b|green light\b)', re.I)


def requires_owner(row):
    if row.get('no_owner_required') or row.get('copy_of'):
        return False
    return (row.get('kind') in ('approval', 'ruling') or
            row.get('kind') in ('answer', 'decision', 'msg') and
            isinstance(row.get('text'), str) and bool(APPROVES.match(row['text'])))


def topics(manifest, rows, at):
    """Last approval and owner per topic; malformed rows never prevent a digest.

    Reuse the sweep/MCP snapshot, capped at 10,000 rows in addressed.window_hours.
    A later explicit owner or claim clears a finding without an alert latch.
    """
    result = {}
    topic_by_id = {}
    cutoff = at - manifest.addressed.window_hours * 3600
    for row in rows[-10000:]:
        if not isinstance(row, dict) or row.get('copy_of'):
            continue
        topic = row.get('topic', 'general')
        if not isinstance(topic, str) or not isinstance(row.get('id'), str) or not isinstance(row.get('ts'), str):
            continue
        try:
            stamp = parse_ts(row['ts']).timestamp()
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if not cutoff <= stamp <= at:
            continue
        if row.get('kind') == 'claim' and topic == 'general' and isinstance(row.get('re'), str):
            topic = topic_by_id.get(row['re'], topic)
        topic_by_id[row['id']] = topic
        item = result.setdefault(topic, dict(owner=None, approval=None, stamp=0))
        owner = row.get('owner') or (row.get('frm') if row.get('kind') == 'claim' else None)
        if isinstance(owner, str) and owner.strip() and owner not in ('all', 'lead'):
            item['owner'] = owner
        if row.get('kind') in ('approval', 'ruling') or requires_owner(row):
            item.update(approval=row if requires_owner(row) else None, stamp=stamp)
    return result


def findings(manifest, rows, at):
    out = []
    for topic, item in topics(manifest, rows, at).items():
        row = item['approval']
        age = at - item['stamp']
        if row and not item['owner'] and age >= duration(manifest.obligations.owner_required_after):
            out.append(dict(event='owner_missing', id=row['id'], topic=topic, age=age,
                            text=f"owner-missing topic={topic} approval={row['id']} age={int(age // 60)}m"))
    return out
