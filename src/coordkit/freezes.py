"""Deploy freeze calendar: list windows, check services, compute cut-off events (orchestration Phase 0.5).

usage: freezes list [--hours 36] [--file F]
       freezes.sh check SERVICE... [--file F]       # exit 0 clear, 75 frozen (freeze refusal code)
       freezes.sh cutoffs [--hours 24] [--file F]   # cut-off events (T-warn before each window) in the horizon
       freezes.sh arm [--hours 24] [--key lead]     # arm those cut-offs as lead-wait deadlines

The calendar is infra/rollout/freezes.toml (see its header). A waiver is the freeze's waiver_env set to a
non-empty value. lead_wait.py fires cut-offs from the calendar on its own; `arm` exists for other keys and for
one-off checks. FREEZES_NOW (RFC3339) overrides the clock in tests.
"""
import argparse
import datetime as dt
import os
import pathlib
import sys

try:
    import tomllib
except ImportError:  # python < 3.11: fail closed with a clear message
    tomllib = None

DEFAULT = None
FROZEN = 75


class FreezeError(Exception):
    pass


def now():
    raw = os.environ.get('FREEZES_NOW')
    if raw:
        return dt.datetime.fromisoformat(raw.replace('Z', '+00:00'))
    return dt.datetime.now(dt.timezone.utc)


def load(path=DEFAULT):
    if tomllib is None:
        raise FreezeError('python >= 3.11 (tomllib) is required to read freezes.toml')
    try:
        doc = tomllib.loads(pathlib.Path(path).read_text())
    except tomllib.TOMLDecodeError as e:
        raise FreezeError(f'{path}: {e}')
    warn = doc.get('defaults', {}).get('warn_minutes', [90, 30])
    out, names = [], set()
    for f in doc.get('freeze', []):
        name = f.get('name')
        if not name or name in names:
            raise FreezeError(f'freeze needs a unique name: {f!r}')
        names.add(name)
        if bool(f.get('daily')) == bool(f.get('start') or f.get('end')):
            raise FreezeError(f'{name}: set exactly one of daily or start/end')
        if not f.get('services'):
            raise FreezeError(f'{name}: services is required ("*" for all)')
        out.append({**f, 'warn_minutes': f.get('warn_minutes', warn)})
    return out


def _hm(text):
    h, m = text.split(':')
    return dt.timedelta(hours=int(h), minutes=int(m))


def windows(freezes, start, end):
    """Concrete (window_start, window_end, freeze) overlapping [start, end)."""
    out = []
    for f in freezes:
        if f.get('daily'):
            a, b = (_hm(x) for x in f['daily'].split('-'))
            day = (start - dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            while day < end:
                ws = day + a
                we = day + b if b > a else day + dt.timedelta(days=1) + b
                if ws < end and we > start:
                    out.append((ws, we, f))
                day += dt.timedelta(days=1)
        else:
            ws = dt.datetime.fromisoformat(str(f['start']).replace('Z', '+00:00'))
            we = dt.datetime.fromisoformat(str(f['end']).replace('Z', '+00:00'))
            if ws < end and we > start:
                out.append((ws, we, f))
    return sorted(out, key=lambda x: x[0])


def covers(freeze, service):
    return '*' in freeze['services'] or service in freeze['services']


def waived(freeze):
    env = freeze.get('waiver_env')
    return bool(env and os.environ.get(env, '').strip() not in ('', '0'))


def active(freezes, at, services):
    """[(freeze, window_end)] covering any of services at `at`, not waived."""
    return [(f, we) for ws, we, f in windows(freezes, at, at + dt.timedelta(seconds=1))
            if ws <= at < we and any(covers(f, s) for s in services) and not waived(f)]


def cutoffs(freezes, start, end):
    """[(when, label)] cut-off events with `when` in [start, end)."""
    out = []
    for ws, _, f in windows(freezes, start, end + dt.timedelta(days=1)):
        for minutes in f['warn_minutes']:
            when = ws - dt.timedelta(minutes=int(minutes))
            if start <= when < end:
                out.append((when, f"cutoff-{f['name']}-T{int(minutes)}m"))
    return sorted(out)


def fmt(t):
    return t.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%MZ')


