"""Review verdicts with durable exact-head enforcement.

A review verdict must be durable and bound to the exact head and result bytes.
Scratch output alone cannot authorize a merge. Starts and ends are journaled.
"""
import argparse
import datetime as dt
import fcntl
import json
import re
import sys

from .manifest import ManifestError

# `VERDICT: X` anywhere, or a line that is only the verdict (including a bare **BLOCK** line).
VERDICT = re.compile(r'VERDICT:\s*\**\s*(SHIP|FIX-FIRST|BLOCK)\b|^[ \t>*_#-]*(SHIP|FIX-FIRST|BLOCK)[ \t*_.]*$', re.I | re.M)
VERDICTS = ('SHIP', 'FIX-FIRST', 'BLOCK')


def ledger_path(manifest):
    if not manifest.review.ledger:
        raise ManifestError('missing_key: review.ledger')
    return manifest.path(manifest.review.ledger)


def entries(manifest, pr=None):
    path = ledger_path(manifest)
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue  # the incumbent ledger has had partial lines; never let one hide the rest
        if row.get('event') == 'end' and (pr is None or str(row.get('pr')) == str(pr)):
            out.append(row)
    return out


def latest(manifest, pr):
    """The newest `end` event for PR, in ledger order, whatever its verdict (a NO-VERDICT or
    unparsed end is newest evidence too, and never lets an older SHIP through)."""
    found = entries(manifest, pr)
    return found[-1] if found else None


def rows_of(manifest):
    path = ledger_path(manifest)
    out = []
    for line in (path.read_text().splitlines() if path.exists() else []):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def closes(end, start):
    """Does this end event close that start? By run id when both carry one, else the legacy key."""
    if end.get('run') or start.get('run'):
        return end.get('run') == start.get('run')
    return all(end.get(k) == start.get(k) for k in ('pr', 'round', 'label', 'head'))


def open_start(manifest, pr, head, out, run=None):
    """The newest start for this PR, head and result file (and run id, if given) with no end yet."""
    import os
    want = os.path.realpath(manifest.path(out)) if out else None
    rows = rows_of(manifest)
    ends = [r for r in rows if r.get('event') == 'end']
    for row in reversed(rows):
        if row.get('event') == 'start' and str(row.get('pr')) == str(pr) and row.get('head') == head \
                and row.get('out') and os.path.realpath(row['out']) == want and (run is None or row.get('run') == run) \
                and not any(closes(e, row) for e in ends):
            return row
    return None


def start(manifest, pr, head, out, label, round_=None, family=None):
    """Ledger `start` event before a review runs: binds the result file to its PR and head."""
    manifest.writable()
    if not re.fullmatch(r'[0-9a-f]{40}', head or ''):
        raise ManifestError('invalid_value: --head must be a 40-hex commit')
    import uuid
    row = {'repo': manifest.project.git_repo, 'pr': int(pr), 'round': round_ or len(entries(manifest, pr)) + 1,
           'head': head, 'since': None, 'mode': 'record', 'label': label, 'family': family or manifest.review.family,
           'ruling': None, 'out': str(manifest.path(out)), 'event': 'start',
           'ts': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
           'run': uuid.uuid4().hex, 'started_at': __import__('time').time()}
    append(manifest, row)
    print(f'REVIEW_STARTED pr={pr} head={head[:9]} label={label} run={row["run"]} out={row["out"]}')
    return row['run']


def append(manifest, row):
    path = ledger_path(manifest)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(json.dumps(row) + '\n')
        stream.flush()
        fcntl.flock(stream, fcntl.LOCK_UN)


def unledgered(manifest, at=None):
    """Result files matching [review].result_globs (modified within 48h) that hold a verdict but are
    named by no ledger row: a review that ran outside the ledger (an unjournaled result file)."""
    import glob, os, time
    at = time.time() if at is None else at
    path = ledger_path(manifest)
    named = set()
    for line in (path.read_text().splitlines() if path.exists() else []):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        # Only an `end` records a verdict, and only for the content it hashed: a reused result file
        # rewritten by a later run (BLOCK, never recorded) is unrecorded again.
        if row.get('out') and row.get('event') == 'end':
            named.add((os.path.realpath(row['out']), row.get('sha256')))
    out = []
    for pattern in manifest.review.result_globs:
        for name in sorted(glob.glob(pattern, recursive=True)):
            try:
                if at - os.path.getmtime(name) > 48*3600:
                    continue
                with open(name, 'rb') as stream:
                    data = stream.read()
                if (os.path.realpath(name), fingerprint(data)) in named:
                    continue
                verdict = verdict_from(data[:200000].decode(errors='replace'))
            except OSError:
                continue
            if verdict and verdict != 'SHIP':  # an unledgered SHIP enables nothing; a BLOCK must be seen
                out.append({'out': os.path.realpath(name), 'verdict': verdict, 'mtime': os.path.getmtime(name)})
    return out


def fingerprint(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()


def live_head(manifest, pr):
    from .merge import gh
    try:
        return json.loads(gh(manifest, 'pr', 'view', pr, '--json', 'headRefOid')).get('headRefOid')
    except Exception:  # unknown head: never release a hold on it
        return None


def verdict_from(text):
    """The last VERDICT line in a review result (the last block wins, like the auditor's JSON)."""
    found = [(a or b).upper() for a, b in VERDICT.findall(text)]
    return found[-1] if found else None


def record(manifest, pr, head, out, label, verdict=None, round_=None, family=None, rc=None, post=True, run=None):
    from . import bus
    manifest.writable()
    if not re.fullmatch(r'[0-9a-f]{40}', head or ''):
        raise ManifestError('invalid_value: --head must be a 40-hex commit')
    # One open, one read: verdict, hash and mtime all describe the same bytes.
    data, mtime = b'', None
    if out:
        import os
        with open(manifest.path(out), 'rb') as stream:
            mtime = os.fstat(stream.fileno()).st_mtime
            data = stream.read()
    text = data.decode(errors='replace')
    found = verdict_from(text)
    if verdict == 'SHIP' and found != 'SHIP':
        # The bus has no authenticated identity, so a SHIP must at least carry its evidence:
        # a result file whose last VERDICT line is SHIP.
        raise ManifestError('review_ship_needs_evidence: --out FILE whose last VERDICT line is SHIP')
    verdict = verdict or found
    begun = open_start(manifest, pr, head, out, run)
    if begun and begun.get('run') and run is None:
        # Concurrent starts can share PR, head and file: an end must name its run.
        raise ManifestError('review_run_required: this review was started with a run id; pass --run')
    if verdict == 'SHIP':
        # Evidence is bound to its PR, head and run: an open ledger `start` for this PR at this head with
        # this result file, a successful run, and a file written after that start.
        import os
        if not begun:
            raise ManifestError(f'review_ship_unbound: no open ledger start for #{pr} at {head[:9]} with {out}')
        if rc != 0:  # None (not given) is not success
            raise ManifestError(f'review_ship_failed_run: rc={rc}')
        if mtime is None or mtime <= begun.get('started_at', float('inf')):
            raise ManifestError('review_ship_stale_file: the result file was not written after its start')
    if verdict not in VERDICTS:
        # A review that ends without a verdict is recorded as such and holds: silence is not SHIP.
        verdict = 'NO-VERDICT'
    previous = [r for r in entries(manifest, pr)]
    row = {'repo': manifest.project.git_repo, 'pr': int(pr), 'round': round_ or len(previous) + 1, 'head': head,
           'since': None, 'mode': 'record', 'label': label, 'family': family or manifest.review.family,
           'ruling': None, 'out': str(manifest.path(out)) if out else None, 'event': 'end',
           'ts': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'), 'rc': rc, 'verdict': verdict}
    if begun:
        row.update(run=begun.get('run'), round=begun.get('round', row['round']), label=begun.get('label', label))
    if out:
        row['sha256'] = fingerprint(data)
    append(manifest, row)
    print(f'REVIEW_RECORDED pr={pr} head={head[:9]} verdict={verdict} label={label}')
    if not post:
        return verdict
    bus.configure(manifest)
    rows = bus.read()
    from .merge import holds_on
    holding = label in holds_on(str(pr), rows)
    summary = f'{row["family"]} {verdict} on #{pr} at {head[:9]} (round {row["round"]})' + (f'; result {row["out"]}' if row['out'] else '')
    if verdict != 'SHIP':
        bus.main(['post', '--from', label, '--to', 'lead', '--kind', 'hold', '--pr', str(pr), '--force',
                  f'Review hold: {summary}. Fix, re-review, and a SHIP at the new head releases it.'], manifest)
    elif holding and live_head(manifest, pr) != head:
        print(f'REVIEW_HOLD_KEPT pr={pr}: SHIP covers {head[:9]}, not the live head', file=sys.stderr)
    elif holding:
        bus.main(['post', '--from', label, '--to', 'lead', '--kind', 'unhold', '--pr', str(pr), '--force',
                  f'Review released: {summary}.'], manifest)
    return verdict


def stale(manifest, at=None, max_minutes=None):
    """Ledger `start` events with no later `end` for the same PR/round/label after max_minutes:
    reviews whose verdict never reached the ledger (when an external runner was used)."""
    import time
    from .process import parse_ts
    at = time.time() if at is None else at
    limit = (max_minutes or manifest.review.end_within_minutes) * 60
    path = ledger_path(manifest)
    rows = []
    for line in (path.read_text().splitlines() if path.exists() else []):
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    ends = [r for r in rows if r.get('event') == 'end']
    out = []
    for r in rows:
        if r.get('event') != 'start' or not r.get('ts'):
            continue
        age = at - parse_ts(r['ts']).timestamp()
        if not any(closes(e, r) for e in ends) and limit <= age <= 48*3600:
            out.append(dict(r, age_minutes=int(age // 60)))
    return out


def main(argv, manifest):
    p = argparse.ArgumentParser(prog='coordkit review', allow_abbrev=False)
    sub = p.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('record', help='record a finished review: ledger end event, hold on BLOCK/FIX-FIRST')
    r.add_argument('--pr', type=int, required=True)
    r.add_argument('--head', required=True, help='the exact 40-hex head the review covered')
    r.add_argument('--label', required=True, help='reviewer label; it posts (and later releases) the hold')
    r.add_argument('--out', help='review result file; its last VERDICT: line is the verdict')
    r.add_argument('--verdict', choices=VERDICTS)
    r.add_argument('--round', type=int)
    r.add_argument('--family')
    r.add_argument('--rc', type=int, required=True, help="the review run's exit code (a SHIP needs 0)")
    r.add_argument('--run', help='run id printed by `review start` (binds this end to that start)')
    s = sub.add_parser('status', help='latest ledger verdict for a PR (read-only)')
    s.add_argument('--pr', type=int, required=True)
    st = sub.add_parser('start', help='ledger start event before a review runs (binds result file to PR and head)')
    st.add_argument('--pr', type=int, required=True)
    st.add_argument('--head', required=True)
    st.add_argument('--label', required=True)
    st.add_argument('--out', required=True)
    st.add_argument('--round', type=int)
    st.add_argument('--family')
    sc = sub.add_parser('scan', help='reviews started but never ended, and result files outside the ledger (read-only)')
    sc.add_argument('--relay', action='store_true', help='append new findings to [addressed].relay_file once each')
    o = p.parse_args(argv)
    if o.cmd == 'start':
        start(manifest, o.pr, o.head, o.out, o.label, o.round, o.family)
        return 0
    if o.cmd == 'scan':
        found = stale(manifest)
        lines = [f"REVIEW_UNRECORDED #{r['pr']} round {r.get('round')} {r.get('label')} at {str(r.get('head'))[:9]} "
                 f"started {r['age_minutes']}m ago, no verdict in the ledger (result: {r.get('out')})" for r in found]
        loose = unledgered(manifest)
        found = found + [dict(f, pr='?', round='?', label='?', head='?') for f in loose]
        lines += [f"REVIEW_UNLEDGERED {f['verdict']} in {f['out']}: a review ran outside the ledger; merge tools "
                  f"cannot see it (record it with coordkit review record)" for f in loose]
        for line in lines[:len(found) - len(loose)]:
            print(line)
        if loose:
            oldest = min(loose, key=lambda f: f['mtime'])['out']
            print(f'REVIEW_UNLEDGERED count={len(loose)} oldest={oldest}')
        if o.relay and lines:
            from .addressed import home, to_relay_file
            from .state import read_json, write_json
            seen_path = home(manifest)/'review-scan.json'
            seen = read_json(seen_path, {}) or {}
            keys = [f"{r['pr']}:{r.get('round')}:{r.get('label')}:{r.get('head')}:{r.get('out')}" for r in found]
            for key, line in zip(keys, lines):
                if key not in seen and to_relay_file(manifest, line):
                    seen[key] = True
            write_json(seen_path, {k: v for k, v in seen.items() if k in keys})
        return 5 if found else 0
    if o.cmd == 'status':
        entry = latest(manifest, o.pr)
        print(json.dumps(entry) if entry else f'REVIEW_NONE pr={o.pr}')
        return 0 if entry and entry.get('verdict') == 'SHIP' else 5
    verdict = record(manifest, o.pr, o.head, o.out, o.label, o.verdict, o.round, o.family, o.rc, run=o.run)
    return 0 if verdict == 'SHIP' else 5
