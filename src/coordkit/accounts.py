"""Read-only view of fleet-published capacity facts; never probes providers."""
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import stat
import time

from .manifest import duration

MAX_BYTES = 1024 * 1024
MAX_ACCOUNTS = 256
MAX_WINDOWS = 32


def _time(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError('invalid timestamp')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamp requires timezone')
    return parsed.astimezone(timezone.utc)


def _utc(value):
    return value.isoformat().replace('+00:00', 'Z')


def _name(value):
    if not isinstance(value, str) or not value or len(value) > 200 or any(ord(c) < 32 for c in value):
        raise ValueError('invalid name')
    try:
        value.encode('utf-8')  # lone surrogates from JSON escapes cannot be served
    except UnicodeEncodeError:
        raise ValueError('invalid name') from None
    return value


def snapshot(manifest, at=None):
    """One bounded snapshot per request; invalid or absent input is unavailable.

    O_NONBLOCK plus fstat rejects pipes/devices without waiting on a writer.
    Both publication and observation age matter: republishing old observations
    cannot make them fresh. No state is persisted and no bus calls are made.
    """
    result = dict(available=False, status='disabled', generated_at=None, accounts=[])
    if not manifest.accounts.facts_file:
        return result
    result['status'] = 'unavailable'
    try:
        path = manifest.path(manifest.accounts.facts_file)
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                return result
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return result
        data = json.loads(raw)
        if not isinstance(data, dict) or type(data.get('schema_version')) is not int or data['schema_version'] != 1:
            return result
        generated = _time(data.get('generated_at'))
        accounts = data.get('accounts')
        if not isinstance(accounts, list) or len(accounts) > MAX_ACCOUNTS:
            return result
        at = time.time() if at is None else at
        max_age = duration(manifest.accounts.max_age)
        def stale(stamp):
            age = at - stamp.timestamp()
            return age < 0 or age > max_age
        rows, ids = [], set()
        for account in accounts:
            id = _name(account['id'])
            if id in ids:
                raise ValueError('duplicate account')
            ids.add(id)
            windows = account['windows']
            if not isinstance(windows, list) or len(windows) > MAX_WINDOWS:
                raise ValueError('invalid windows')
            reading, names = [], set()
            for window in windows:
                name = _name(window['name'])
                if name in names:
                    raise ValueError('duplicate window')
                names.add(name)
                ratio = window['used_ratio']
                if type(ratio) not in (int, float) or not math.isfinite(ratio) or not 0 <= ratio <= 1:
                    raise ValueError('invalid ratio')
                observed = _time(window['observed_at'])
                resets = window['resets_at']
                reading.append(dict(name=name, used_pct=round(ratio * 100, 2),
                                    resets_at=_utc(_time(resets)) if resets is not None else None,
                                    observed_at=_utc(observed), stale=stale(generated) or stale(observed)))
            rows.append(dict(id=id, windows=reading))
        return dict(available=True, status='ok', generated_at=_utc(generated), accounts=sorted(rows, key=lambda row: row['id']))
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RuntimeError):
        return result


def account_windows(data, account):
    """Join config-dir basenames to declared ids or statusline discovery ids.

    Prefer the declared id if both exist; ambiguous discovered ids do not join.
    """
    if not account:
        return []
    id = account.removeprefix('.')
    direct = [row for row in data['accounts'] if row['id'] == id]
    discovered = [row for row in data['accounts']
                  if row['id'].startswith('undeclared:claude_statusline:')
                  and Path(row['id'].split(':', 2)[2]).name.removeprefix('.') == id]
    matches = direct or discovered
    return [{k: window[k] for k in ('name', 'used_pct', 'resets_at', 'stale')}
            for window in matches[0]['windows']] if len(matches) == 1 else []


def main(argv, manifest):
    from .ops import parser_for
    args = parser_for('accounts').parse_args(argv)
    data = snapshot(manifest)
    if args.json:
        print(json.dumps(data))
    elif not data['available']:
        print('accounts: ' + data['status'])
    else:
        print('ACCOUNT\tWINDOWS (used %, resets_at, observed_at, stale)')
        for row in data['accounts']:
            windows = '; '.join(f"{w['name']}: {w['used_pct']:g}% resets_at={w['resets_at']} "
                                f"observed_at={w['observed_at']} stale={str(w['stale']).lower()}" for w in row['windows'])
            print(row['id'] + '\t' + (windows or '(no windows)'))
    return 0
