"""Manifest-driven lead waiter; source protocol ported from lead_wait.py."""
import argparse
import datetime as dt
import fcntl
import json
import os
import pathlib
import re
import subprocess
import sys
import time

from . import freezes
from .anxcli import AnxClient, past_promises
from .actl import AgentctlClient
from .manifest import duration, ManifestError
from .process import ToolError, run
from .state import name_key, read_json
from .wait_policy import DEFAULT_BUS_KINDS, wake_worthy

CONFIG = None
ROOT = DEFAULT_BUS = DEFAULT_FREEZES = None
TERMINAL_STATES = {'completed', 'failed', 'cancelled', 'canceled', 'orphaned', 'timed_out'}
WAKE_KINDS = {'terminal', 'attention', 'stall', 'stalled', 'relaunched', 'autocontinued'}
DEFERRED_BUS_KINDS = {'ask', 'handoff', 'apply_request', 'ruling'}
DEDUPE_BUS_KINDS = DEFERRED_BUS_KINDS  # never fail: a changed count in a fail is news
BUS_SOURCES = ('bus', 'bus-repeat', 'bus-status', 'bus-dup')
GH = os.environ.get('GH', 'gh')
ANX = os.environ.get('ANX', 'anx')
ANX_NO_ANSWER_RC = 8  # anx await timed out: nothing new


def now():
    if os.environ.get('LEAD_WAIT_NOW'):  # tests
        return parse_ts(os.environ['LEAD_WAIT_NOW'])
    return dt.datetime.now(dt.timezone.utc)


def parse_ts(text):
    try:
        value = dt.datetime.fromisoformat(str(text).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def iso(value):
    return value.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def private_dir(path):
    """Create `path` (and parents) and make it owner-only: agentctl >= 0.16 refuses file subscriptions whose directory
    is not owner-only (0700)."""
    path = pathlib.Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if path.stat().st_mode & 0o077 and path.stat().st_uid == os.getuid():
        path.chmod(0o700)
    return path


def default_events():
    return str(CONFIG.paths.events)


class State:
    """Per-key cursors under <root>/<key>/state.json, written atomically under a lock."""

    def __init__(self, key):
        if key in ('', '.', '..'):
            raise ManifestError('invalid_wait_key')
        self.dir = ROOT / name_key(key)
        self.path = self.dir / 'state.json'
        try:
            self.data = json.loads(self.path.read_text())
        except FileNotFoundError:
            self.data = {}
        for name, empty in (('offsets', {}), ('seen', []), ('ci', {}), ('deadlines', []), ('fired', []),
                            ('config', {}), ('held', [])):
            self.data.setdefault(name, empty)
        self._loaded_deadlines = list(self.data['deadlines'])

    def save(self, only=None):
        """Write state under the lock. `only` (field names) rewrites just those fields over the on-disk state.

        A long wait can hold a stale snapshot while arm-deadline adds another deadline.
        Deadlines merge three ways under the lock: what other processes added since this one loaded is kept; what another process consumed (in this load, gone from disk, or fired) stays consumed; what
        this process consumed stays consumed. arm-deadline saves only=('deadlines',), so it never rolls back a
        waiter's cursors (offsets, seen, ci) with its stale snapshot.
        """
        CONFIG.writable()
        private_dir(self.dir)
        self.data['seen'] = self.data['seen'][-2000:]
        self.data['stale'] = self.data.get('stale', [])[-500:]
        self.data['stale_times'] = dict(sorted(self.data.get('stale_times', {}).items(), key=lambda pair:pair[1])[-500:])
        tmp = self.path.with_suffix('.tmp')
        with open(self.dir / '.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                disk = json.loads(self.path.read_text())
            except FileNotFoundError:
                disk = {}
            on_disk = disk.get('deadlines') or []
            fired = list(self.data['fired'])
            fired += [f for f in disk.get('fired') or [] if f not in fired]
            mine_new = [d for d in self.data['deadlines'] if d not in self._loaded_deadlines]
            merged = [d for d in self.data['deadlines'] if d in on_disk or d in mine_new]
            merged += [d for d in on_disk if d not in self._loaded_deadlines and d not in merged]
            self.data['deadlines'] = [d for d in merged if f"{d['label']}@{d['at']}" not in fired]
            self.data['fired'] = fired[-500:]
            out = self.data if only is None else dict(disk, **{k: self.data[k] for k in (*only, 'fired')})
            tmp.write_text(json.dumps(out, indent=1, sort_keys=True))
            tmp.replace(self.path)
            self._loaded_deadlines = list(self.data['deadlines'])


# --- lane events -------------------------------------------------------------------------------------------
def event_of(record):
    """(dedupe_key, line) for a wake-worthy lane event, else None."""
    e = record.get('event') if isinstance(record.get('event'), dict) else record
    exec_id = e.get('execution_id') or e.get('exec') or ''
    kind = (e.get('kind') or '').lower()
    state = (e.get('state') or '').lower()
    label = e.get('label') or '+'.join(e.get('labels') or []) or record.get('label') or exec_id or '?'
    terminal = kind == 'terminal' or (kind in ('', 'lane_exit') and state in TERMINAL_STATES)
    if terminal:
        key = f'{exec_id or label}:terminal'
        rc = e.get('rc', record.get('rc'))
        out = e.get('out') or record.get('out') or ''
        extra = (f' rc={rc}' if rc is not None else '') + (f' out={out}' if out else '')
        if e.get('card'):
            extra += f" card={e['card']} handed to {e.get('handoff') or 'nobody'} ({e.get('handoff_how', '?')})"
        return key, f'LEAD_WAIT lane {label} {state or "terminal"}{extra} exec={exec_id or "-"}'
    if kind in WAKE_KINDS:
        reason = e.get('reason') or (e.get('payload') or {}).get('reason') or state
        key = record.get('event_dedupe_key') or e.get('event_dedupe_key') or e.get('dedupe_key') or f'{exec_id}:{kind}:{reason}:{e.get("sequence", "")}'
        return key, f'LEAD_WAIT lane {label} {kind} {reason} exec={exec_id or "-"}'
    return None


def scan_events(state, files, consume):
    out = []
    seen = set(state.data['seen'])
    missing = state.data.setdefault('missing_sources', [])
    for name in files:
        path = pathlib.Path(name)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            # A pull from another host has died, so that host's lanes can't wake anyone. One file is the default
            # local case, which may simply not exist yet.
            if len(files) > 1 and name not in missing:
                if consume:
                    missing.append(name)
                out.append((f'missing:{name}', f'LEAD_WAIT lane events-source attention source_missing {name} exec=-'))
            continue
        if name in missing and consume:
            missing.remove(name)
        identity = [path.stat().st_dev, path.stat().st_ino]
        identities = state.data.setdefault('identities', {})
        offset = state.data['offsets'].get(name, 0)
        if identities.get(name, identity) != identity:
            offset = 0
        if consume:
            identities[name] = identity
        if offset > size:  # truncated or replaced: start over, dedupe keys prevent repeats
            offset = 0
        with path.open('rb') as f:
            f.seek(offset)
            chunk = f.read()
        complete = chunk[:chunk.rfind(b'\n') + 1]  # never consume a half-written line
        for raw in complete.splitlines():
            try:
                record = json.loads(raw)
            except ValueError as exc:
                raise ValueError(f'invalid source JSON: {exc}') from exc
            hit = event_of(record) if isinstance(record, dict) else None
            if hit and hit[0] not in seen:
                seen.add(hit[0])
                out.append(hit)
        if consume:
            state.data['offsets'][name] = offset + len(complete)
    if consume:
        state.data['seen'].extend(k for k, _ in out if not k.startswith('missing:'))
    return [line for _, line in out]


# --- bus -----------------------------------------------------------------------------------------------------
def near_dup_key(frm, topic, kind, text):
    """Text with times stripped. SHAs and ids stay, so a new head or another card or PR is never a duplicate."""
    text = re.sub(r'\b\d{1,2}:\d{2}(:\d{2})?z?\b', '', text.lower())
    return f"{frm}|{topic}|{kind}|{' '.join(text.split())[:100]}"


def scan_bus(state, bus, to, kinds, consume, tiers=None):
    """kinds is a set, or None for every kind. tiers is the config dict when wake tiers are on."""
    path = pathlib.Path(bus)
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return []
    start = state.data.get('bus_line')
    if start is None or start > len(lines):
        start = len(lines)  # first run: do not replay history
        state.data['bus_line'] = start
    out = []
    # Latched repeats: an identical (frm, topic, kind, first 80 chars) post already delivered in
    # the last REPEAT_WINDOW seconds comes back as a `bus-repeat` line, which muted_by counts instead of waking.
    seen = state.data.setdefault('bus_seen', {})
    near = state.data.setdefault('bus_near', {})
    for raw in lines[start:]:
        try:
            r = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f'invalid bus JSON: {exc}') from exc
        kind = r.get('kind', '')
        addressed = r.get('to') == to or r.get('owner') == to or (r.get('to') == 'all' and kind == 'fail')
        if addressed and wake_worthy(r, kinds) and r.get('frm') != to:
            text = ' '.join((r.get('text') or '').split())[:160]
            key = f"{r.get('frm', '?')}|{r.get('topic', '')}|{kind}|{text[:80]}"
            ts = parse_ts(r.get('ts') or '')
            t = ts.timestamp() if ts else time.time()
            first = seen.get(key)
            source = 'bus'
            if first is not None and 0 <= t - first < REPEAT_WINDOW:
                source = 'bus-repeat'
            elif consume:
                seen[key] = t
            frm = r.get('frm', '?')
            if tiers and source == 'bus' and frm not in (tiers.get('never_defer_from') or []):
                source = ride_source(r, kind, frm, t, near, consume)
            out.append(f"LEAD_WAIT {source} {r.get('frm', '?')} {kind}/{r.get('topic', '')} id={r.get('id', '')} {text}")
    if consume:
        state.data['bus_line'] = len(lines)
        # Prune on the bus's own clock (the post timestamps the window compares), not the wall clock: replayed or
        # older posts must not lose their dedupe keys just because time has passed.
        cutoff = max(seen.values(), default=time.time()) - 2 * REPEAT_WINDOW
        state.data['bus_seen'] = {k: v for k, v in seen.items() if v >= cutoff}
        state.data['bus_near'] = {k: v for k, v in near.items() if v >= cutoff}
    return out


def ride_source(r, kind, frm, t, near, consume):
    """'bus-dup' or 'bus-status' when this post should ride along rather than wake, else 'bus'."""
    if kind not in DEDUPE_BUS_KINDS:
        return 'bus'
    key = near_dup_key(frm, r.get('topic', ''), kind, r.get('text') or '')
    first = near.get(key)
    if first is not None and 0 <= t - first < REPEAT_WINDOW:
        return 'bus-dup'
    if consume:
        near[key] = t
    return 'bus-status' if kind == 'handoff' and not r.get('evidence') else 'bus'


def tier_of(line, cfg):
    """'immediate', 'debounced' or 'ride' for a LEAD_WAIT line. Anything unrecognised is immediate."""
    if not cfg.get('debounce'):
        return 'immediate'
    parts = line.split()
    source = parts[1] if len(parts) > 1 else ''
    if source in ('bus-status', 'bus-dup'):
        return 'ride'
    if source == 'bus' and len(parts) > 3:
        if parts[2] in (cfg.get('never_defer_from') or []):
            return 'immediate'
        return 'debounced' if parts[3].split('/', 1)[0] in DEFERRED_BUS_KINDS else 'immediate'
    if source == 'lane' and len(parts) > 3:
        return 'debounced' if parts[3] in ('completed', 'relaunched', 'autocontinued') else 'immediate'
    if source == 'ci' and len(parts) > 3:
        return 'immediate' if parts[3] == 'failed' else 'debounced'
    return 'immediate'


def hold_limit(tier, settle, debounce, ride_max):
    """Seconds a held event may wait. With tiers off, a tier saved by an earlier stream no longer delays anything."""
    if not debounce:
        return settle
    return {'debounced': debounce, 'ride': ride_max}.get(tier, settle)


def flush_due(held, at, settle, debounce, ride_max):
    """True when the held events should wake the lead at epoch `at`."""
    return any(at >= h['t'] + hold_limit(h['tier'], settle, debounce, ride_max) for h in held)


# --- deadlines -------------------------------------------------------------------------------------------------
def due_deadlines(state, freezes_path, at, consume):
    items = [(d['at'], d['label']) for d in state.data['deadlines']]
    if freezes_path:
        try:
            items += [(iso(when), label) for when, label in freezes.cutoffs(freezes.load(freezes_path), at - dt.timedelta(hours=2), at + dt.timedelta(minutes=1))]
        except (OSError, ValueError, freezes.FreezeError) as e:
            return [f'LEAD_WAIT_SOURCE_ERROR freezes {e}']
    fired = set(state.data['fired'])
    out = []
    for when, label in sorted(items):
        key = f'{label}@{when}'
        moment = parse_ts(when)
        if moment and moment <= at and key not in fired:
            out.append(f'LEAD_WAIT deadline {label} at={when}')
            fired.add(key)
            if consume:
                state.data['fired'].append(key)
    if consume:  # armed deadlines that fired are done
        state.data['deadlines'] = [d for d in state.data['deadlines'] if f"{d['label']}@{d['at']}" not in fired]
    return out


def arm_deadline(state, when, label):
    moment = parse_ts(when)
    if moment is None:
        raise ValueError(f'bad deadline time {when!r} (RFC3339 UTC)')
    entry = {'at': iso(moment), 'label': label}
    if entry not in state.data['deadlines']:
        state.data['deadlines'].append(entry)
    return entry


# --- CI ----------------------------------------------------------------------------------------------------------
def ci_verdict(pr, repo):
    """(head, verdict, detail): verdict in green|failed|merged|closed|pending. Raises on gh failure."""
    view = run([GH, 'pr', 'view', str(pr), '-R', repo, '--json', 'state,headRefOid'],
                          timeout=60)
    if view.returncode != 0:
        raise RuntimeError((view.stderr or view.stdout).strip()[:160])
    info = json.loads(view.stdout)
    head = info.get('headRefOid') or ''
    if info.get('state') in ('MERGED', 'CLOSED'):
        return head, info['state'].lower(), ''
    checks = run([GH, 'pr', 'checks', str(pr), '-R', repo, '--json', 'name,bucket'],
                            timeout=60)
    if checks.returncode not in (0,1,8):
        raise RuntimeError(checks.stderr or 'gh checks failed')
    try:
        rows = json.loads(checks.stdout or '[]')
    except ValueError:
        raise RuntimeError((checks.stderr or 'unparseable gh pr checks').strip()[:160])
    failed = [r['name'] for r in rows if r.get('bucket') in ('fail', 'cancel')]
    if failed:
        return head, 'failed', ','.join(failed[:3])
    if rows and all(r.get('bucket') in ('pass','skipping') for r in rows):
        return head, 'green', f'{len(rows)} checks'
    return head, 'pending', ''


def scan_ci(state, prs, repo, consume, errors):
    out = []
    for pr in prs:
        try:
            head, verdict, detail = ci_verdict(pr, repo)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as e:
            if f'ci{pr}' not in errors:
                errors.add(f'ci{pr}')
                out.append(f'LEAD_WAIT_SOURCE_ERROR ci #{pr} {e}')
            continue
        if verdict == 'pending':
            continue
        mark = f'{head}:{verdict}'
        if state.data['ci'].get(str(pr)) != mark:
            out.append(f'LEAD_WAIT ci #{pr} {verdict} head={head} {detail}'.rstrip())
            if consume:
                state.data['ci'][str(pr)] = mark
    return out


def scan_anx(errors, state=None):
    out = []
    try:
        try:
            answers = AnxClient(CONFIG).call('await', '--answers', '--timeout', '1s', '--settle', '0').get('answers', [])
        except ToolError as exc:
            if exc.exit_code != ANX_NO_ANSWER_RC:
                raise
            answers = []
        for item in answers:
            out.append(f"LEAD_WAIT anx-answer ask={item.get('ask_id')} answer={json.dumps(item.get('answer'))}")
        orient = AnxClient(CONFIG).call('orient')
        for item in past_promises(orient):
            out.append(f"LEAD_WAIT past-promise {json.dumps(item, sort_keys=True)}")
        errors.discard('anx')
    except (ToolError, OSError, ValueError, TypeError) as exc:
        if 'anx' not in errors:
            out.append(f'LEAD_WAIT_SOURCE_ERROR anx {exc}')
        errors.add('anx')
    if state is not None:
        seen = state.data.setdefault('anx_seen', [])
        fresh = [line for line in out if line.startswith('LEAD_WAIT_SOURCE_ERROR') or line not in seen]
        seen.extend(line for line in fresh if not line.startswith('LEAD_WAIT_SOURCE_ERROR'))
        state.data['anx_seen'] = seen[-1000:]
        return fresh
    return out


# --- driver --------------------------------------------------------------------------------------------------------------
def pr_list(values):
    out = []
    for value in values or []:
        for part in str(value).replace(',', ' ').split():
            part = part.lstrip('#')
            if not part.isdigit():
                raise ValueError(f'bad --ci PR {part!r}')
            if int(part) not in out:
                out.append(int(part))
    return out


def config_from(o):
    return {'events': o.events or [default_events()], 'ci': pr_list(o.ci), 'repo': o.repo, 'bus': o.bus,
            'bus_to': o.bus_to, 'bus_kinds': o.bus_kinds, 'freezes': o.freezes, 'mute_from': o.mute_from, 'mute_label': o.mute_label,
            'mute_topic': getattr(o, 'mute_topic', []), 'debounce': getattr(o, 'debounce', 0),
            'never_defer_from': getattr(o, 'never_defer_from', []), 'anx_answers': getattr(o, 'anx_answers', False)}


def muted_by(line, cfg):
    """The mute rule a LEAD_WAIT line matches, or None."""
    parts = line.split()
    if len(parts) > 2 and parts[1] in BUS_SOURCES and parts[2] in (cfg.get('mute_from') or []):
        return parts[2]  # an explicitly muted sender keeps its own count
    if len(parts) > 2 and parts[1] == 'bus-repeat':
        return f'repeat:{parts[2]}'
    if len(parts) > 3 and parts[1] in BUS_SOURCES:
        topic = parts[3].split('/', 1)[1] if '/' in parts[3] else ''
        if f'{parts[2]}:{topic}' in (cfg.get('mute_topic') or []):
            return f'topic:{parts[2]}:{topic}'
    if len(parts) > 2 and parts[1] == 'lane':
        for prefix in cfg.get('mute_label') or []:
            if parts[2].startswith(prefix):
                return f'lane:{prefix}'
    return None


def sweep(state, cfg, at, consume, with_ci, errors, with_anx=False):
    lines = scan_events(state, cfg['events'], consume)
    if cfg.get('bus') and cfg['bus'] != 'none':
        kinds = None if cfg['bus_kinds'] == 'all' else set(cfg['bus_kinds'].split(','))
        lines += scan_bus(state, cfg['bus'], cfg['bus_to'], kinds, consume, cfg if cfg.get('debounce') else None)
    freezes_path = None if cfg.get('freezes') in (None, 'none') else cfg['freezes']
    lines += due_deadlines(state, freezes_path, at, consume)
    if with_ci and cfg.get('ci'):
        lines += scan_ci(state, cfg['ci'], cfg['repo'], consume, errors)
    if with_anx and consume and cfg.get('anx_answers'):
        lines += scan_anx(errors, state)
    return lines


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def cmd_wait(o):
    state = State(o.key)
    for spec in o.deadline:
        when, _, label = spec.partition('=')
        try:
            arm_deadline(state, when, label or f'deadline-{when}')
        except ValueError as e:
            print(f'LEAD_WAIT_USAGE {e}')
            return 2
    try:
        cfg = config_from(o)
    except ValueError as e:
        print(f'LEAD_WAIT_USAGE {e}')
        return 2
    pidfile = state.dir / 'waiter.pid'
    private_dir(state.dir)
    waiter_lock = (state.dir/'waiter.lock').open('a')
    try:
        fcntl.flock(waiter_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        waiter_lock.close()
        print(f'LEAD_WAIT_ALREADY_ARMED key={o.key}', flush=True)
        return 4
    state.data['config'] = cfg
    state.save()
    pidfile.write_text(str(os.getpid()))
    deadline = time.monotonic() + o.timeout * 60
    errors, next_ci, next_anx = set(), 0.0, 0.0
    next_stale = 0.0
    muted = state.data.setdefault('muted', {})
    held = state.data['held']

    def flush():
        for h in held:
            print(h['line'], flush=False)
        if muted:
            print('LEAD_WAIT_MUTED ' + ' '.join(f'{k}={v}' for k, v in sorted(muted.items())), flush=False)
        print(f'LEAD_WAIT_BATCH events={len(held)}', flush=True)
        held.clear()
        muted.clear()
        state.save()

    try:
        while True:
            with_ci = time.monotonic() >= next_ci
            if with_ci:
                next_ci = time.monotonic() + o.ci_poll
            with_anx = time.monotonic() >= next_anx
            if with_anx:
                next_anx = time.monotonic() + o.anx_poll
            found = sweep(state, cfg, now(), True, with_ci, errors, with_anx)
            if o.stale_every > 0 and time.monotonic() >= next_stale:
                next_stale = time.monotonic() + o.stale_every
                found += scan_stale(state, cfg['events'], True, cfg.get('bus'))
            for line in found:
                if line.startswith('LEAD_WAIT_SOURCE_ERROR'):
                    print(line, flush=True)
                    continue
                rule = muted_by(line, cfg)
                if rule:
                    muted[rule] = muted.get(rule, 0) + 1
                else:
                    held.append({'line': line, 'tier': tier_of(line, cfg), 't': time.time()})
            state.save()
            if not o.stream and time.monotonic() >= deadline and not held:
                if muted:
                    print('LEAD_WAIT_MUTED ' + ' '.join(f'{k}={v}' for k, v in sorted(muted.items())), flush=True)
                    muted.clear()
                    state.save()
                print(f'LEAD_WAIT_TIMEOUT {o.timeout:g}m', flush=True)
                return 3
            if held and (flush_due(held, time.time(), o.settle, o.debounce, o.ride_max)
                         or (not o.stream and time.monotonic() >= deadline)):
                flush()
                if not o.stream:
                    return 0
            limits = [h['t'] + hold_limit(h['tier'], o.settle, o.debounce, o.ride_max) - time.time() for h in held]
            limits.append(o.poll)
            if not o.stream:
                limits.append(deadline - time.monotonic())
            time.sleep(max(0.05, min(limits)))
    finally:
        try:
            if pidfile.read_text().strip() == str(os.getpid()):
                pidfile.unlink()
        except FileNotFoundError:
            print('LEAD_WAIT_PIDFILE_MISSING', file=sys.stderr)
        waiter_lock.close()


def cmd_pending(o):
    state = State(o.key)
    cfg = state.data.get('config') or config_from(o)
    if not cfg.get('events'):
        cfg['events'] = [default_events()]
    lines = [line for line in sweep(state, cfg, now(), False, False, set()) if not muted_by(line, cfg)]
    pid = (state.dir / 'waiter.pid').read_text().strip() if (state.dir / 'waiter.pid').exists() else ''
    armed = pid_alive(pid)
    held = [h['line'] for h in state.data['held']]
    if o.brief:
        kinds = {}
        for line in lines:
            kinds.setdefault(line.split()[1] if len(line.split()) > 1 else '?', []).append(line)
        parts = [f'{len(v)} {k}' for k, v in sorted(kinds.items())]
        head = '; '.join(parts) if parts else 'nothing unread'
        if held:
            head += f'; {len(held)} held for the next wake'
        print(f"lead-wait[{o.key}]: {head}; waiter {'armed' if armed else 'NOT ARMED'}"
              + (f" (deadlines armed: {len(state.data['deadlines'])})" if state.data['deadlines'] else ''))
        for line in lines[:8]:
            print('  ' + line[:200])
    else:
        for line in held + lines:
            print(line)
        print(f"LEAD_WAIT_PENDING count={len(held) + len(lines)} armed={'yes' if armed else 'no'}")
    return 0


def cmd_arm(o):
    state = State(o.key)
    try:
        entry = arm_deadline(state, o.at, o.label)
    except ValueError as e:
        print(f'LEAD_WAIT_USAGE {e}')
        return 2
    state.save(only=('deadlines',))  # never roll back a running wait's cursors
    print(f"LEAD_WAIT_ARMED deadline {entry['label']} at={entry['at']} key={o.key}")
    return 0


def in_flight(files, records=False):
    """{exec id: label} (or {exec id: launched record} with records=True) launched by launch_lane with no terminal
    record yet."""
    launched, done = {}, set()
    for name in files:
        try:
            lines = pathlib.Path(name).read_text().splitlines()
        except OSError:
            continue
        for raw in lines:
            try:
                record = json.loads(raw)
            except ValueError as exc:
                raise ValueError(f'invalid source JSON: {exc}') from exc
            e = record.get('event') if isinstance(record.get('event'), dict) else record
            exec_id = e.get('execution_id')
            if not exec_id:
                continue
            if e.get('kind') in ('launched', 'relaunched', 'autocontinued'):  # a relaunch/auto-continue is the lane's new execution
                launched[exec_id] = e if records else (e.get('label') or exec_id)
                if e.get('previous_execution_id'):  # ... and replaces the one that died or parked
                    done.add(e['previous_execution_id'])
            elif event_of(record) and (e.get('kind') == 'terminal' or (e.get('state') or '') in TERMINAL_STATES):
                done.add(exec_id)
    return {k: v for k, v in launched.items() if k not in done}


REPEAT_WINDOW = 3600  # seconds a delivered bus post's exact repeats stay muted
AGENTCTL = os.environ.get('AGENTCTL', 'agentctl')
DEAD_LIVENESS = {'exited', 'unreachable', 'lost', 'orphaned'}


ACTIVE_SECONDS = 90 * 60  # a bus post or output this recent means the lane is alive even if agentctl can't see it


def bus_activity(bus_path):
    """{sender: (last post epoch, [(handoff epoch, text)])} from the bus file; {} when there is no bus."""
    out = {}
    if not bus_path or bus_path == 'none':
        return out
    try:
        with open(bus_path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                    t = dt.datetime.fromisoformat(r['ts'].replace('Z', '+00:00')).timestamp()
                except (ValueError, KeyError):
                    continue
                last, handoffs = out.get(r.get('frm'), (0, []))
                if r.get('kind') == 'handoff':
                    handoffs = handoffs + [t]
                out[r.get('frm')] = (max(last, t), handoffs)
    except FileNotFoundError:
        return out
    return out


def scan_stale(state, files, consume, bus=None):
    """Lanes launched by launch_lane whose execution is over (or lost) with no terminal record.

    Not stale: a lane that posted a handoff on the bus after its launch has handed off, which is
    a terminal record; and an execution agentctl only reports as unreachable (a detached lane whose watcher is gone)
    that posted or wrote output in the last 90 minutes is alive but unobserved."""
    out, fired = [], set(state.data.setdefault('stale', []))
    activity = bus_activity(bus)
    now_t = time.time()
    records = in_flight(files, records=True)
    stale_times = state.data.setdefault('stale_times', {})
    if consume:
        state.data['stale_times'] = stale_times = {key:stamp for key,stamp in stale_times.items() if key in records}
    for exec_id, rec in records.items():
        try:
            info = AgentctlClient(CONFIG).status(exec_id)
        except (ToolError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
            out.append(f'LEAD_WAIT_SOURCE_ERROR stale {exec_id} {exc}')
            info = None
        if not info:
            continue
        st, live = (info.get('state') or '').lower(), (info.get('liveness') or '').lower()
        if st not in TERMINAL_STATES and live not in DEAD_LIVENESS:
            continue
        label = rec.get('label') or exec_id
        try:
            launched_at = dt.datetime.fromisoformat(str(rec.get('ts', '')).replace('Z', '+00:00')).timestamp()
        except ValueError:
            launched_at = 0
        last_post, handoffs = activity.get(label, (0, []))
        if any(h >= launched_at for h in handoffs):
            if consume:
                stale_times.pop(exec_id, None)
            continue
        out_file = rec.get('out')
        try:
            out_mtime = os.path.getmtime(out_file) if out_file else 0
        except OSError:
            out_mtime = 0
        if st not in TERMINAL_STATES and now_t - max(last_post, out_mtime) < ACTIVE_SECONDS:
            continue
        key = f'{exec_id}:{st}:{live}'
        if exec_id in stale_times and now_t-stale_times[exec_id] < duration(CONFIG.wait.stale_alert_after):
            continue
        card = rec.get('card')
        out.append(f"LEAD_WAIT stale lane {rec.get('label') or exec_id} state={st or '?'} liveness={live or '?'} "
                   f"exec={exec_id} no terminal record (watcher gone)"
                   + (f' card={card} (ownerless: re-home it)' if card else ''))
        if consume:
            if key not in fired:
                state.data['stale'].append(key)
            stale_times[exec_id] = now_t
    return out


def cmd_register(o):
    session = o.session or os.environ.get('CLAUDE_CODE_SESSION_ID') or ''
    if not session:
        print('LEAD_WAIT_USAGE no --session and no CLAUDE_CODE_SESSION_ID')
        return 2
    state = State(o.key)
    private_dir(state.dir)
    (state.dir / 'lead-session').write_text(session + '\n')
    print(f'LEAD_WAIT_LEAD_REGISTERED key={o.key} session={session}')
    return 0


def cmd_ask_guard(o):
    try:
        if os.environ.get('LEAD_WAIT_ASK_GUARD', 'on') == 'off':
            return 0
        hook = json.loads(sys.stdin.read() or '{}')
        if hook.get('tool_name') != 'AskUserQuestion':
            return 0
        lead = (ROOT / name_key(o.key) / 'lead-session').read_text().strip()
        if not lead or hook.get('session_id') != lead:
            return 0
        cfg = (State(o.key).data.get('config') or {})
        running = in_flight(cfg.get('events') or [default_events()])
        if not running:
            return 0
        names = ', '.join(sorted(running.values())[:5]) + (' ...' if len(running) > 5 else '')
        reason = (f'{len(running)} lane(s) in flight ({names}). A modal question blocks every waiter until the operator '
                  'answers. Use `anx ask "<decision>?" --subject-ref card:<x> --recommend <option> '
                  '--by <when>` and keep working, or state "PROCEED at HH:MM unless you object" for reversible '
                  'actions. LEAD_WAIT_ASK_GUARD=off overrides.')
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                                 'permissionDecisionReason': reason}}))
    except Exception:  # noqa: BLE001 - a hook must fail open
        return 0
    return 0


def parser():
    p = argparse.ArgumentParser(prog='lead_wait.sh', description='See module doc.')
    p.add_argument('--key', default=os.environ.get('LEAD_WAIT_KEY', 'lead'))
    sub = p.add_subparsers(dest='cmd')
    w = sub.add_parser('wait')
    pend = sub.add_parser('pending')
    pend.add_argument('--brief', action='store_true')
    arm = sub.add_parser('arm-deadline')
    arm.add_argument('at')
    arm.add_argument('label')
    reg = sub.add_parser('register-lead')
    reg.add_argument('--session')
    guard = sub.add_parser('ask-guard')
    for q in (w, pend, arm, reg, guard):
        q.add_argument('--key', default=argparse.SUPPRESS)
    for q in (w, pend):
        q.add_argument('--timeout', type=float, default=55, help='minutes')
        q.add_argument('--events', action='append', default=None)
        q.add_argument('--ci', action='append', default=[], help='PR number(s); repeatable or comma-separated')
        q.add_argument('--repo', default=CONFIG.project.git_repo)
        q.add_argument('--bus', default=str(DEFAULT_BUS))
        q.add_argument('--bus-to', default='lead')
        q.add_argument('--bus-kinds', default=DEFAULT_BUS_KINDS, help="comma list, or 'all'")
        q.add_argument('--mute-from', action='append', default=[], help='bus sender whose posts never wake (counted)')
        q.add_argument('--mute-topic', action='append', default=[], metavar='FRM:TOPIC',
                       help="one sender's posts on one topic never wake (counted), e.g. worker-a:maintenance")
        q.add_argument('--mute-label', action='append', default=[], help='lane label prefix whose events never wake')
        q.add_argument('--settle', type=float, default=10, help='seconds to keep collecting after the first event')
        q.add_argument('--debounce', type=float, default=0, help='seconds a debounced event may wait; 0 turns wake tiers off')
        q.add_argument('--ride-max', type=float, default=900, help='seconds a ride-along event may wait')
        q.add_argument('--never-defer-from', action='append', default=[], help='bus sender whose posts are always immediate')
        q.add_argument('--stream', action='store_true', help='never exit; print each batch inline (for the Monitor tool)')
        q.add_argument('--stale-every', type=float, default=300, help='seconds between stale-lane checks (0 = off)')
        q.add_argument('--deadline', action='append', default=[])
        q.add_argument('--freezes', default=str(DEFAULT_FREEZES))
        q.add_argument('--poll', type=float, default=15, help='seconds')
        q.add_argument('--ci-poll', type=float, default=60, help='seconds')
        q.add_argument('--anx-answers', action='store_true', help="wake on answers to this session's anx asks")
        q.add_argument('--anx-poll', type=float, default=30, help='seconds')
    return p


def main(argv=None, manifest=None):
    global CONFIG, ROOT, DEFAULT_BUS, DEFAULT_FREEZES, GH, AGENTCTL
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ('policy', 'wake-worthy'):
        from .wait_policy import main as policy_main
        return policy_main(argv, manifest)
    CONFIG = manifest
    ROOT, DEFAULT_BUS = manifest.paths.root/'wait', manifest.paths.bus_log
    DEFAULT_FREEZES = str(manifest.path(manifest.wait.freezes)) if manifest.wait.freezes else 'none'
    GH, AGENTCTL = os.environ.get('GH', 'gh'), os.environ.get('AGENTCTL', 'agentctl')
    argv = list(sys.argv[1:] if argv is None else argv)
    if not any(a in ('wait', 'pending', 'arm-deadline', 'register-lead', 'ask-guard') for a in argv):
        argv.insert(0, 'wait')
    p = parser()
    for action in p._subparsers._group_actions[0].choices.values():
        action.set_defaults(timeout=manifest.wait.timeout_minutes, settle=manifest.wait.settle,
                            debounce=manifest.wait.debounce, ride_max=manifest.wait.ride_max,
                            poll=manifest.wait.poll, ci_poll=manifest.wait.ci_poll, anx_poll=manifest.wait.anx_poll,
                            anx_answers=manifest.wait.anx_answers, ci=[str(pr) for pr in manifest.wait.ci],
                            bus_kinds=manifest.wait.kinds)
    o = p.parse_args(argv)
    if o.cmd not in ('pending', 'ask-guard'):
        manifest.writable()
    if o.cmd == 'pending':
        return cmd_pending(o)
    if o.cmd == 'arm-deadline':
        return cmd_arm(o)
    if o.cmd == 'register-lead':
        return cmd_register(o)
    if o.cmd == 'ask-guard':
        return cmd_ask_guard(o)
    return cmd_wait(o)


if __name__ == '__main__':
    sys.exit(main())
