"""Owned obligations: every handoff, ask and auditor flag has an owner and an ack.

Pure derivation from bus rows plus PR states; the sweep sends one agent wake and an hourly digest and
`coordkit obligations report` prints the open set read-only (safe in shadow mode).
"""
import argparse
import hashlib
import json
import re
import sys
import time

from .manifest import duration
from .merge import PR_MENTION
from .process import parse_ts

KINDS = ('handoff', 'ask')
# Rows coordkit itself writes about obligations are reminders, never new obligations.
OWN_PREFIXES = ('wake-', 'alert-', 'reconcile-', 'answer-')
FLAG = re.compile(r'\bF-[0-9a-f]{6}\b')
# A blanket disposition must say why: `dismissed: <reason>` or `all flags owner=<x>`.
CLOSE_ALL = re.compile(r'\bdismissed:\s*\S|\ball flags owner=\S', re.I)


def flag_id(title):
    return 'F-' + hashlib.sha256(' '.join(title.lower().split()).encode()).hexdigest()[:6]


def prs(row):
    """PR numbers in first-mention order: explicit --pr fields, then the text."""
    out = [str(p) for p in row.get('prs') or []]
    out += [m.group(1) for m in PR_MENTION.finditer(row.get('text') or '')]
    return list(dict.fromkeys(out))


URGENT = re.compile(r'^\s*\[?URGENT\b')


def priority(row):
    """urgent | normal: an explicit `priority` field, else a leading URGENT in the text."""
    if row.get('priority') in ('urgent', 'normal'):
        return row['priority']
    return 'urgent' if URGENT.match(row.get('text') or '') else 'normal'


def due_after(manifest, kind, level):
    """Seconds an obligation may wait for its owner before it alerts."""
    cfg = manifest.obligations
    if kind != 'ask':
        return duration(cfg.alert_after)  # handoffs and auditor flags keep the pickup ladder
    return duration(cfg.urgent_deadline if level == 'urgent' else cfg.ask_deadline)


def is_flag_report(manifest, row):
    return row.get('topic') == 'missed-work' or row.get('frm') in manifest.obligations.flag_sources


def candidate(manifest, row):
    if row.get('kind') not in KINDS or row.get('to') in (None, '', 'all'):
        return False
    if str(row.get('id', '')).startswith(OWN_PREFIXES):
        return False
    # A handoff that names no PR and is not an auditor report is a status update, not work.
    return (row['kind'] == 'ask' or bool(prs(row)) or is_flag_report(manifest, row)
            or bool(row.get('handoff_bounce') or row.get('handoff_degraded'))
            or any(str(row.get(k, '')).startswith('repo:') for k in ('ref', 'evidence')))


def open_obligations(manifest, rows, at, pr_state=lambda number: None):
    """[{id, kind, frm, owner, since, age, prs, flags}] still waiting for an owner's pickup."""
    from . import bus
    bus.configure(manifest)
    book = bus.aliases()
    canonical = lambda name: bus.resolve(name, book)
    replies = {}
    for row in rows:
        if row.get('re'):
            replies.setdefault(row['re'], []).append(row)
    found = []
    for index, row in enumerate(rows):
        if not row.get('id') or not row.get('ts') or not candidate(manifest, row):
            continue
        owner, since = canonical(row.get('owner') or row['to']), parse_ts(row['ts']).timestamp()
        numbers = prs(row)
        flags = sorted(set(FLAG.findall(row.get('text') or ''))) if is_flag_report(manifest, row) else []
        closed = None
        for reply in replies.get(row['id'], []):
            # Copies (the bus's idle-recipient lead copy, addressed escalations) are never replies:
            # an alert's lead copy carries --re and used to close the obligation it alerted on.
            if reply.get('frm') == row['frm'] or str(reply.get('id', '')).startswith(OWN_PREFIXES) \
                    or reply.get('copy_of') or reply.get('escalation'):
                continue
            from .addressed import assignees
            from .handoffs import portable
            if (row['kind'] == 'handoff' and portable(row)
                    and not (reply.get('handoff_verified') == row['id'] or reply.get('handoff_bounce'))):
                continue  # old/foreign replies cannot silently claim portable payloads they never probed
            if (reply.get('kind') == 'ack' or reply.get('ack')) and canonical(reply.get('frm')) not in {canonical(n) for n in assignees(dict(row, owner=owner), manifest)}:
                continue  # only the assigned reader can pick up or reassign work
            if flags:
                if canonical(reply.get('frm')) != owner:
                    continue  # only the flag owner dispositions flags
                text = reply.get('text') or ''
                flags = [] if CLOSE_ALL.search(text) and not FLAG.search(text) else [f for f in flags if f not in text]
                closed = None if flags else 'disposition:' + reply['id']
            elif reply.get('kind') == 'ack' and reply.get('owner') and canonical(reply['owner']) != canonical(reply['frm']):
                # Reassignment: the new owner must pick it up; the clock restarts.
                owner, since = canonical(reply['owner']), parse_ts(reply['ts']).timestamp()
            else:
                closed = 'reply:' + reply['id']
        if closed:
            continue
        # Copies and coordkit's own rows never close implicitly either (an alert's lead copy is frm=lead).
        later = [r for r in rows[index+1:] if not (r.get('copy_of') or r.get('escalation')
                                                   or str(r.get('id', '')).startswith(OWN_PREFIXES))]
        if flags:
            # A newer report from the same auditor carries its recurring flags forward.
            newer = {f for r in later if r.get('frm') == row['frm'] and is_flag_report(manifest, r)
                     for f in FLAG.findall(r.get('text') or '')}
            flags = [f for f in flags if f not in newer]
            if not flags:
                continue
        if numbers and not flags:
            if any(r.get('frm') == owner and set(prs(r)) & set(numbers) for r in later):
                continue
            if any(r.get('frm') == row['frm'] and r.get('kind') in KINDS and set(prs(r)) & set(numbers) for r in later):
                continue  # superseded by a newer handoff/ask from the same sender
            if all(pr_state(n) in ('MERGED', 'CLOSED') for n in numbers):
                continue  # any named PR still open keeps the work open
        if row.get('subject') and any(r.get('frm') == owner and r.get('subject') == row['subject'] for r in later):
            continue
        level = 'normal' if flags else priority(row)
        found.append({'id':row['id'], 'kind':row['kind'], 'frm':row['frm'], 'owner':owner, 'since':since,
                      'age':max(0, at-since), 'prs':numbers, 'flags':flags, 'text':(row.get('text') or '')[:200],
                      'priority':level, 'due_after':due_after(manifest, 'flag' if flags else row['kind'], level)})
    return found


def line(item):
    """One alert line: id, owner, age."""
    what = ' '.join(['#'+p for p in item['prs'][:2]] + item['flags'][:3])
    urgent = 'URGENT ' if item.get('priority') == 'urgent' else ''
    return f"{urgent}{item['kind']} {item['id']} from {item['frm']} owner={item['owner']} age={item['age']/3600:.1f}h {what}".strip()


def stage(manifest, item):
    """None, 'wake' or ('alert', n): the ladder step this obligation has reached."""
    cfg = manifest.obligations
    age, alert_at = item['age'], item.get('due_after', duration(cfg.alert_after))
    if age >= alert_at:
        return ('alert', int((age-alert_at) // duration(cfg.realert_after)))
    return 'wake' if duration(cfg.ack_after) < alert_at and age >= duration(cfg.ack_after) else None


def gh_states(manifest, numbers):
    from .merge import gh
    states = {}
    for number in sorted(set(numbers)):
        try:
            states[number] = json.loads(gh(manifest, 'pr', 'view', number, '--json', 'state')).get('state')
        except Exception:  # unknown state keeps the obligation open; never closes it
            states[number] = None
    return states


def report(manifest, rows=None, at=None, window_hours=None, since=None):
    from . import bus
    bus.configure(manifest)
    rows = bus.read() if rows is None else rows
    at = time.time() if at is None else at
    floor = max(since or 0, at-window_hours*3600 if window_hours else 0)
    recent = lambda r: r.get('ts') and parse_ts(r['ts']).timestamp() >= floor
    named = [n for r in rows if recent(r) and candidate(manifest, r) for n in prs(r)]
    states = gh_states(manifest, named)
    # Replies of any age still close; only obligations raised inside the window are reported.
    return [item for item in open_obligations(manifest, rows, at, states.get) if item['since'] >= floor]


def main(argv, manifest):
    parser = argparse.ArgumentParser(prog='coordkit obligations')
    parser.add_argument('command', choices=['report'])
    parser.add_argument('--window-hours', type=float, default=48)
    parser.add_argument('--min-age', default='', help='only list obligations at least this old')
    parser.add_argument('--due', action='store_true',
                        help='only list obligations past their own deadline (urgent 5m, ask 15m, handoff/flag alert_after)')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    items = [i for i in report(manifest, window_hours=args.window_hours) if (not args.min_age or i['age'] >= duration(args.min_age))
             and (not args.due or i['age'] >= i['due_after'])]
    items.sort(key=lambda i: -i['age'])
    if args.json:
        json.dump(items, sys.stdout, indent=1)
        print()
    else:
        for item in items:
            print('OBLIGATION ' + line(item))
        print(f'OBLIGATIONS open={len(items)}')
    return 5 if items else 0
