"""Derived, append-only closure and liveness views for bus obligations.

Nothing here writes the log. Acknowledgement, supersession, orphaning and stale
RUNNING state are computed from the retained rows and a caller-supplied clock.
"""
import time

from .merge import PR_MENTION
from .process import parse_ts

DEFAULT_INACTIVE_AFTER = 72 * 3600


def name_key(name):
    return (name or '').casefold()


def timestamp(row):
    try:
        return parse_ts(row['ts']).timestamp()
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def named_prs(row):
    numbers = [str(number) for number in row.get('prs') or []]
    numbers.extend(match.group(1) for match in PR_MENTION.finditer(row.get('text') or ''))
    return frozenset(numbers)


def activity(rows):
    """Latest valid bus-post timestamp by case-insensitive sender name."""
    latest = {}
    for row in rows:
        stamp = timestamp(row)
        if stamp is not None and row.get('frm'):
            key = name_key(row['frm'])
            latest[key] = max(stamp, latest.get(key, stamp))
    return latest


def _reply_closes(row, reply, kind):
    """Legacy ack/blocker closure plus a direct owner reply for rulings."""
    if kind == 'blocker':
        # The pre-existing blocker contract: any later decision/answer, regardless
        # of sender, closes it. Copies and escalations are not excluded here.
        return reply.get('kind') in ('decision', 'answer') and not reply.get('auto_received')
    # Legacy ruling ack: any later ack closes, including a forced ack from a
    # non-owner. This is the behavior that existed before derived closure.
    if reply.get('kind') == 'ack':
        return True
    if reply.get('copy_of') or reply.get('escalation') or reply.get('auto_received'):
        return False
    return name_key(reply.get('frm')) == name_key(row.get('owner'))


def _superseder(candidates, index, numbers):
    for later_index, later, later_numbers in candidates:
        if later_index <= index:
            continue
        if shared := numbers & later_numbers:
            return later, sorted(shared)
    return None


def _fallback(owner, retired, fallbacks, default_fallback):
    def redirect(name):
        record = retired.get(name_key(name), {})
        return record.get('owner') or fallbacks.get(name_key(name))

    target = redirect(owner) or default_fallback or 'lead'
    # A malformed retirement record must not route an orphan back to its dead owner.
    if name_key(target) == name_key(owner):
        target = default_fallback or 'lead'
    # Follow retirement redirects and declared fallbacks until a live name is
    # reached. A cycle is not a recipient: fall through to the coordinator/lead.
    seen = {name_key(owner)}
    while name_key(target) in retired:
        key = name_key(target)
        if key in seen:
            target = None
            break
        seen.add(key)
        target = redirect(target)
        if not target:
            target = None
            break
    if target is None or name_key(target) in retired:
        for candidate in (default_fallback, 'lead'):
            if candidate and name_key(candidate) not in retired:
                return candidate
        return 'lead'
    return target


def derive(rows, at=None, *, inactive_after=DEFAULT_INACTIVE_AFTER, retired=None,
           fallbacks=None, default_fallback='lead'):
    """Return {open, orphaned, closed} for owned rulings and blockers.

    ``retired`` and ``fallbacks`` are mappings keyed by lane name (case insensitive
    during lookup). A retirement record's ``owner`` is its explicit redirect.
    ``fallbacks`` maps an owner to its declared fallback. No other I/O is needed,
    which makes this function suitable for detached historical snapshots.

    Orphaning is not deletion or acknowledgement. An item leaves the actionable open
    set and retains its reason, inactivity evidence and one-time fallback target.
    A missing owner activity record uses the obligation timestamp as its baseline;
    this puts never-seen, old owners in ``unknown`` rather than leaving them immortal.
    """
    at = time.time() if at is None else at
    retired = {name_key(name): record or {} for name, record in (retired or {}).items()}
    fallbacks = {name_key(name): target for name, target in (fallbacks or {}).items()}
    last_activity = activity(rows)
    replies = {}
    rulings_by_pair = {}
    for index, row in enumerate(rows):
        if row.get('re'):
            replies.setdefault(row['re'], []).append((index, row))
        if row.get('kind') == 'ruling':
            numbers = named_prs(row)
            if numbers:
                key = (name_key(row.get('frm')), name_key(row.get('owner')))
                rulings_by_pair.setdefault(key, []).append((index, row, numbers))
    out = {'open': [], 'orphaned': [], 'closed': []}
    for index, row in enumerate(rows):
        kind = row.get('kind')
        if kind not in ('ruling', 'blocker') or not row.get('id'):
            continue
        # Ownerless rulings stay ignored as before. An ownerless blocker is still
        # closable by rule 4 and otherwise remains open, never orphaned.
        if kind == 'ruling' and not row.get('owner'):
            continue
        resolution = None
        for reply_index, reply in replies.get(row['id'], []):
            if reply_index > index and _reply_closes(row, reply, kind):
                resolution = ('acknowledged' if reply.get('kind') == 'ack' else 'owner_reply'
                              if kind == 'ruling' else 'answered')
                break
        superseded = None
        if kind == 'ruling':
            key = (name_key(row.get('frm')), name_key(row.get('owner')))
            numbers = named_prs(row)
            if numbers:
                superseded = _superseder(rulings_by_pair.get(key, []), index, numbers)
        if resolution:
            reason, by = resolution, reply.get('id', '')
            closed = dict(row, closure_reason=reason, closed_by=by)
            out['closed'].append(closed)
            continue
        if superseded:
            later, shared = superseded
            out['closed'].append(dict(row, closure_reason='superseded', closed_by=later['id'],
                                      shared_prs=shared))
            continue
        if not row.get('owner'):
            # No fallback owner exists to chase, so an ownerless blocker is ordinary open.
            out['open'].append(row)
            continue
        retired_record = retired.get(name_key(row['owner']))
        last = last_activity.get(name_key(row['owner']))
        since = timestamp(row)
        baseline = last if last is not None else since
        silent = baseline is not None and at - baseline >= inactive_after
        if retired_record is not None or silent:
            if retired_record is not None:
                reason = 'retired'
            elif last is None:
                reason = 'unknown'
            else:
                reason = 'silent'
            orphan = dict(row, orphaned=True, orphan_reason=reason,
                          owner_last_activity=last, inactive_seconds=max(0, at-baseline),
                          fallback=_fallback(row['owner'], retired, fallbacks, default_fallback))
            out['orphaned'].append(orphan)
        else:
            out['open'].append(row)
    return out


def latest_states(rows, at=None, *, inactive_after=DEFAULT_INACTIVE_AFTER):
    """Latest states with effective_state=STALE for abandoned RUNNING rows.

    The recorded ``state`` remains RUNNING. Callers must use the annotation rather
    than rewriting history or treating the lane as actively running.
    """
    at = time.time() if at is None else at
    last_activity = activity(rows)
    latest = {}
    for row in rows:
        if row.get('kind') == 'state' and row.get('subject'):
            latest[(row.get('frm'), row.get('subject'))] = row
    out = {}
    for key, row in latest.items():
        stamp = (last_activity.get(name_key(row.get('frm')))
                 if row.get('state') == 'RUNNING' else None)
        if stamp is not None and at - stamp >= inactive_after:
            row = dict(row, effective_state='STALE', stale=True,
                       owner_last_activity=stamp, inactive_seconds=max(0, at-stamp),
                       stale_reason='lane_inactive')
        else:
            row = dict(row, effective_state=row.get('state'), stale=False)
        out[key] = row
    return out
