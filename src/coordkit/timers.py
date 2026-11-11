"""Scheduled wakes: one bus row to one lane at a set time.

A lane with no process between turns cannot hold a timer, and a sleeping shell
is not a record. A timer is a small file; the periodic wake scan appends its
row when it is due, and the ordinary wake path does the rest. Delivery is at
least once: a row carries the timer's id, so a repeat after a crash is visible.
"""
import argparse
import datetime as dt
import json
import os
import re
import secrets
import time

from .errors import ManifestError
from .state import read_json, write_json

MAX_AHEAD = 7 * 86400
MAX_PER_LANE = 64
MAX_FIRE = 32  # per scan; the rest fire on the next one


def directory(manifest):
    return manifest.paths.bus_log.parent/'state/wakes/timers'


def _locked(manifest):
    from .routing import locked
    return locked(directory(manifest)/'timers.lock')


def pending(manifest):
    rows = []
    for path in sorted(directory(manifest).glob('*.json')):
        try:
            row = read_json(path, None)
        except (ValueError, UnicodeError, OSError):
            row = None
        if (isinstance(row, dict) and isinstance(row.get('id'), str) and isinstance(row.get('lane'), str)
                and type(row.get('due')) in (int, float) and isinstance(row.get('text'), str)):
            rows.append((path, row))
    return rows


def parse_due(at=None, after=None, now=None):
    from .duration import duration
    now = time.time() if now is None else now
    if (at is None) == (after is None):
        raise ManifestError('timer_needs_one_of: --at or --in')
    if after is not None:
        due = now + duration(after)
    else:
        try:
            stamp = dt.datetime.fromisoformat(str(at).replace('Z', '+00:00'))
        except ValueError:
            raise ManifestError('invalid_timer_time: use an ISO time with a zone, like 2030-01-02T03:04:00Z') from None
        if stamp.tzinfo is None:
            raise ManifestError('invalid_timer_time: the time needs a zone (Z or an offset)')
        due = stamp.timestamp()
    if not now < due <= now + MAX_AHEAD:
        raise ManifestError('invalid_timer_time: must be in the future and within 7 days')
    return due


def schedule(manifest, lane, text, *, at=None, after=None, frm='', now=None):
    from . import bus
    from .wake import refuse_retired, registered
    manifest.writable()
    bus.configure(manifest)
    lane = bus.resolve(lane)
    if lane not in registered(manifest):
        raise ManifestError('lane_not_registered: ' + lane)
    refuse_retired(manifest, lane)
    text = ' '.join(str(text or '').split())
    if not text or len(text) > manifest.bus.text_cap:
        raise ManifestError(f'invalid_timer_text: 1..{manifest.bus.text_cap} characters')
    if frm and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', frm):
        raise ManifestError('invalid_timer_sender')
    now = time.time() if now is None else now
    due = parse_due(at, after, now)
    with _locked(manifest):
        if sum(1 for _, row in pending(manifest) if row['lane'] == lane) >= MAX_PER_LANE:
            raise ManifestError('timer_limit: too many pending timers for ' + lane)
        ident = secrets.token_hex(6)
        write_json(directory(manifest)/f'{ident}.json',
                   {'id': ident, 'lane': lane, 'due': due, 'text': text, 'frm': frm or lane, 'created': now})
    return ident, due


def cancel(manifest, ident):
    manifest.writable()
    if not re.fullmatch(r'[0-9a-f]{12}', ident or ''):
        raise ManifestError('invalid_timer_id')
    with _locked(manifest):
        path = directory(manifest)/f'{ident}.json'
        if not path.exists():
            return False
        path.unlink()
    return True


def stamp(value):
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def fire(manifest, now=None):
    """Append the row of every due timer. Called by the wake scan; returns the ids fired.

    A timer is renamed before its row is appended and removed after. A crash in
    between leaves the renamed file, and the next call appends that row again.
    """
    from . import bus
    from .wake import retired
    if not directory(manifest).is_dir():
        return []
    now = time.time() if now is None else now
    fired = []
    with _locked(manifest):
        claimed = [(path, read_json(path, None)) for path in sorted(directory(manifest).glob('*.firing'))]
        for path, row in pending(manifest):
            if row['due'] <= now and len(claimed) < MAX_FIRE:
                target = path.with_suffix('.firing')
                os.replace(path, target)
                claimed.append((target, row))
        if not claimed:
            return []
        bus.configure(manifest)
        for path, row in claimed:
            if isinstance(row, dict) and not retired(manifest, row.get('lane', '')):
                bus.append([dict(ts=dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(timespec='seconds'),
                                 id='wake-at-' + row['id'], origin='coordkit', frm='coordkit', to=row['lane'],
                                 topic='coordkit-timer', kind='handoff',
                                 text=f"Timer set by {row.get('frm') or row['lane']} at {stamp(row.get('created', now))}: "
                                      f"{row['text']}"[:manifest.bus.text_cap])], project=False)
                fired.append(row['id'])
            path.unlink(missing_ok=True)
    return fired


def main(argv, manifest):
    parser = argparse.ArgumentParser(prog='coordkit wake-at', allow_abbrev=False)
    parser.add_argument('lane', nargs='?')
    parser.add_argument('text', nargs='?')
    parser.add_argument('--at')
    parser.add_argument('--in', dest='after')
    parser.add_argument('--from', dest='frm', default=os.environ.get('COORDKIT_AS', ''))
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--cancel')
    opts = parser.parse_args(argv)
    if opts.cancel:
        print('TIMER_CANCELLED ' + opts.cancel if cancel(manifest, opts.cancel) else 'TIMER_ABSENT ' + opts.cancel)
        return 0
    if opts.list:
        for _, row in sorted(pending(manifest), key=lambda item: item[1]['due']):
            if not opts.lane or row['lane'] == opts.lane:
                print(f"TIMER {row['id']} {stamp(row['due'])} to={row['lane']} from={row.get('frm', '')} {json.dumps(row['text'])}")
        return 0
    if not opts.lane or not opts.text:
        parser.error('give a lane and the row text, or --list, or --cancel ID')
    ident, due = schedule(manifest, opts.lane, opts.text, at=opts.at, after=opts.after, frm=opts.frm)
    print(f'TIMER_SET {ident} {stamp(due)} to={opts.lane}')
    return 0
