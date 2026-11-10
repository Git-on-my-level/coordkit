#!/usr/bin/env python3
"""Measure the closure derivation on a detached JSONL bus snapshot.

This never discovers a manifest, opens a live bus, uses a coordkit CLI entry point,
or writes retired-lane state. The optional output file is selected by the caller.
"""
import argparse
import collections
import datetime as dt
import json
from pathlib import Path

from coordkit import closure


def incumbent_closed_ids(rows, kinds):
    """Read-only equivalent of the supplied incumbent's copy-aware closure index."""
    closed, refs, later_acks = set(), set(), set()
    blocker_refs = set()
    copies = collections.defaultdict(list)
    for row in reversed(rows):
        rid = row.get('id')
        if not row.get('copy_of') and (
                rid in refs or (row.get('kind') in ('ruling', 'handoff') and rid in later_acks)):
            closed.add(rid)
        if row.get('copy_of'):
            copies[row['copy_of']].append(rid)
        if isinstance(row.get('re'), str):
            if row.get('kind') in ('decision', 'answer') or (row.get('kind') == 'ack' and row.get('closed') is True):
                blocker_refs.add(row['re'])
            if row.get('kind') in kinds or (row.get('kind') == 'ack' and row.get('closed') is True):
                refs.add(row['re'])
            elif row.get('kind') == 'ack':
                later_acks.add(row['re'])
    pending = collections.deque(closed)
    while pending:
        for rid in copies[pending.popleft()]:
            if rid not in closed:
                closed.add(rid)
                pending.append(rid)
    return closed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    parser.add_argument('--open-rulings', type=Path, required=True,
                        help='reference open list; authoritative because the frozen coordkit comparison predates copy/close semantics')
    parser.add_argument('--inactive-after', default='72h', help='hours with an h suffix (snapshot replay only)')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not args.inactive_after.endswith('h') or float(args.inactive_after[:-1]) <= 0:
        parser.error('--inactive-after must be a positive number of hours')
    rows = [json.loads(line) for line in args.snapshot.read_text().splitlines() if line.strip()]
    at = max(closure.timestamp(row) for row in rows if closure.timestamp(row) is not None)
    inactive_after = float(args.inactive_after[:-1]) * 3600
    view = closure.derive(rows, at, inactive_after=inactive_after, default_fallback='lead')
    supplied = json.loads(args.open_rulings.read_text())
    legacy_ids = {row['id'] for row in supplied}
    legacy_rulings = [row for row in rows if row.get('id') in legacy_ids]
    if len(supplied) != len(legacy_ids) or len(legacy_rulings) != len(legacy_ids) \
            or any(row.get('kind') != 'ruling' or not row.get('owner') for row in legacy_rulings):
        raise SystemExit('open list contains duplicate, missing, non-ruling or ownerless rows')
    owners = {row['id']: row['owner'] for row in rows if row.get('kind') == 'ruling' and row.get('owner')}
    ruled_closed = incumbent_closed_ids(
        [row for row in rows if row.get('kind') != 'answer' or owners.get(row.get('re')) == row.get('frm')],
        ('ack', 'answer'))
    derived_legacy_ids = {row['id'] for row in rows
                          if row.get('kind') == 'ruling' and row.get('owner') and row.get('id') not in ruled_closed}
    if derived_legacy_ids != legacy_ids:
        raise SystemExit(f'open list differs from reference derivation: supplied={len(legacy_ids)} derived={len(derived_legacy_ids)}')
    selected = {group: [row for row in view[group] if row.get('kind') == 'ruling' and row.get('id') in legacy_ids]
                for group in ('open', 'orphaned', 'closed')}
    oldest = sorted(selected['open'], key=lambda row: row['ts'])[:20]
    latest = closure.latest_states(rows, at, inactive_after=inactive_after)
    running = [row for row in latest.values() if row.get('state') == 'RUNNING']
    blocker_closed = incumbent_closed_ids(rows, ('decision', 'answer'))
    blockers = [row for row in rows if row.get('kind') == 'blocker' and row.get('id') not in blocker_closed]
    blocker_ids = {row['id'] for row in blockers}
    result = {
        'snapshot_at': dt.datetime.fromtimestamp(at, dt.timezone.utc).isoformat(),
        'inactive_after': args.inactive_after,
        'rulings': {
            'before_open': len(legacy_ids),
            'after_open': len(selected['open']),
            'orphaned': len(selected['orphaned']),
            'orphan_reasons': dict(collections.Counter(row['orphan_reason'] for row in selected['orphaned'])),
            'closed': len(selected['closed']),
            'closure_reasons': dict(collections.Counter(row['closure_reason'] for row in selected['closed'])),
            'oldest_open': [{key: row.get(key) for key in ('id', 'ts', 'frm', 'owner', 'subject', 'text')}
                            for row in oldest],
        },
        'blockers': {
            'before_open': len(blockers),
            'after_open': len([row for row in view['open'] if row.get('kind') == 'blocker'
                               and row.get('id') in blocker_ids]),
            'orphaned': len([row for row in view['orphaned'] if row.get('kind') == 'blocker'
                             and row.get('id') in blocker_ids]),
            'closed': len([row for row in view['closed'] if row.get('kind') == 'blocker'
                           and row.get('id') in blocker_ids]),
        },
        'latest_running_states': {
            'total': len(running),
            'running': sum(not row['stale'] for row in running),
            'stale': sum(row['stale'] for row in running),
        },
        'ack_required_field_present_on_legacy_open': sum('ack_required' in row for row in legacy_rulings),
    }
    rendered = json.dumps(result, indent=2, sort_keys=True) + '\n'
    if args.output:
        args.output.write_text(rendered)
    else:
        print(rendered, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
