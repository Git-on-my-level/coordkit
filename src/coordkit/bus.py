"""Append-only typed coordination bus, extracted from the proven protocol."""
import hashlib
import io
import stat
import tempfile
from .state import name_key, read_json
import argparse, datetime as dt, fcntl, json, os, pathlib, re, secrets, subprocess, sys, collections, time
from .manifest import ManifestError
from .bus_protocol import KINDS, STATES, BLOCKER_CLOSERS, WAKE_KINDS, TEXT_CAP
from .process import ToolError, parse_ts

CONFIG = None
BUS = STATE = None
IDLE_HOURS = 3
LIVE_BUS_MINUTES = 120
NO_MIRROR = False


def configure(manifest, no_mirror=False):
    global CONFIG, BUS, STATE, IDLE_HOURS, TEXT_CAP, NO_MIRROR, _EXECUTIONS
    CONFIG = manifest
    BUS, STATE = manifest.paths.bus_log, manifest.paths.bus_log.parent / 'state'
    IDLE_HOURS, TEXT_CAP = manifest.bus.idle_hours, manifest.bus.text_cap
    NO_MIRROR, _EXECUTIONS = no_mirror, None


FYI_REMIND_HOURS = 6  # an fyi identical to the sender's last fyi to that recipient on that topic is held this long
SUBJECT = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:#/@+-]{0,99}$')
# Structured signals. Waiters that trigger writes match
# the `signal` field of kind=signal posts only.
SIGNAL_NAME = re.compile(r'^[A-Z][A-Z0-9_]{1,63}$')
def now(): return dt.datetime.now(dt.timezone.utc)
def since(s):
    if not s: return None
    m = re.fullmatch(r'(\d+)([mhd])', s)
    if not m:
        raise ManifestError(f'invalid_since: {s}')
    return now() - dt.timedelta(**{{'m': 'minutes', 'h': 'hours', 'd': 'days'}[m.group(2)]: int(m.group(1))})
REQUIRED = ('ts', 'frm', 'to', 'kind')
EPOCH_TS = '1970-01-01T00:00:00+00:00'
_LEGACY = {'id': 0, 'invalid': 0, 'reported': False}
def normalize(index, row):
    """The single read boundary for rows another writer may have shaped differently (legacy rows may lack IDs). Positions never move, because line-count cursors index the row list. A row keeps its
    content and only missing or invalid required fields are filled: `id` -> `legacy-L<line>`, `ts` -> the epoch,
    `frm`/`to`/`kind` -> ''. Content is never dropped, so a malformed hold still vetoes a merge (fail closed).
    A line that is not an object becomes an inert `legacy_invalid` row. In memory only; the log is never rewritten."""
    if not isinstance(row, dict):
        _LEGACY['invalid'] += 1
        return {'id': f'legacy-L{index}', 'ts': EPOCH_TS, 'frm': '', 'to': '', 'kind': 'legacy_invalid', 'topic': '',
                'text': '', 'legacy': 'invalid', 'v': 1}
    fixed = {f: (EPOCH_TS if f == 'ts' else '') for f in REQUIRED if not isinstance(row.get(f), str)}
    if 'ts' not in fixed:
        try: parse_ts(row['ts'])
        except (ValueError, TypeError): fixed['ts'] = EPOCH_TS
    if not isinstance(row.get('id'), str) or not row['id']:
        fixed['id'] = f'legacy-L{index}'
    row = {'topic': 'general', 'text': '', 'v': 1, **row}
    if not fixed:
        return row
    _LEGACY['id' if set(fixed) == {'id'} else 'invalid'] += 1
    return dict(row, **fixed, legacy=','.join(sorted(fixed)))
def parse_lines(lines, start=0):
    """[(index, normalized row)] for lines[start:]."""
    out = []
    for i in range(start, len(lines)):
        try: out.append((i, normalize(i, json.loads(lines[i]))))
        except ValueError:
            print(f'BUS_INVALID_JSON line={i + 1} (skipped)', file=sys.stderr)
            out.append((i, normalize(i, None)))
    if (_LEGACY['id'] or _LEGACY['invalid']) and not _LEGACY['reported']:
        _LEGACY['reported'] = True
        print(f"BUS_LEGACY_ROWS synthesized_id={_LEGACY['id']} invalid={_LEGACY['invalid']} (normalized in memory)",
              file=sys.stderr)
    return out
def adopted_row(row, since):
    """True when a row counts toward obligations: coordkit wrote it, or another writer (an incumbent still
    posting) wrote it at or after the adoption baseline. since=None (never adopted) counts every row."""
    return (since is None or row.get('origin') == 'coordkit' or 'text_hash' in row
            or parse_ts(row['ts']).timestamp() >= since)
def read(cut=None):
    return read_path(BUS, cut)
def read_path(path, cut=None, *, complete=True):
    """Read committed physical lines without changing CLI configuration.

    The `complete` keyword remains for caller compatibility. Every reader now
    retries a non-newline-terminated tail, even when it contains valid JSON.
    """
    if not path.exists(): return []
    data = path.read_bytes()
    data = data[:data.rfind(b'\n') + 1]
    # Split on physical newlines, not Unicode separators inside message text.
    lines = data.decode('utf-8', errors='replace').split('\n')[:-1]
    rows = [r for _, r in parse_lines(lines)]
    return [r for r in rows if parse_ts(r['ts']) >= cut] if cut else rows
def read_from(start):
    """(line count, [(index, row)...]) for rows at or after line `start`; the log is append-only, so a line count
    is a stable per-reader cursor."""
    from .tail import TailReader
    # Shadow readers never persist hints into the watched bus state.
    checkpoint = STATE / 'tail.offset.json' if CONFIG.mode != 'shadow' else None
    total, lines = TailReader(BUS, checkpoint).read(start)
    indexed = []
    for index, line in lines:
        indexed.extend((index, row) for _, row in parse_lines([line], 0)
                       for row in [dict(row, id=f'legacy-L{index}') if row.get('id') == 'legacy-L0' else row])
    return total, indexed
def cursor_path(lane, kind): return STATE / 'cursors' / f"{name_key(lane)}.{kind}"
def get_position(paths, default):
    """Select progress in memory; only locked consumers may write cursors."""
    paths = tuple(dict.fromkeys(paths))
    positions = []
    for path in paths:
        try:
            value = int(path.read_text().strip())
        except (FileNotFoundError, ValueError):
            continue
        if value >= 0:
            positions.append(value)
    if not positions:
        return default
    if BUS.exists():
        total, _ = read_from(10 ** 18)
        positions = [value for value in positions if value <= total]
    if not positions:
        return default
    return max(positions)


def get_cursor(lane, kind, default):
    old = CONFIG.paths.root / 'state' / 'cursors' / f'{name_key(lane)}.{kind}'
    return get_position((old, cursor_path(lane, kind)), default)
def set_cursor(lane, kind, value):
    # Compatibility window: advance the rollback reader before the new reader.
    # If the locations coincide, perform only one replacement.
    old = CONFIG.paths.root / 'state' / 'cursors' / f'{name_key(lane)}.{kind}'
    for path in dict.fromkeys((old, cursor_path(lane, kind))):
        replace_text(path, f'{value}\n')
def replace_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        tmp = pathlib.Path(stream.name)
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    tmp.replace(path)


class ExactParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **dict(kwargs, allow_abbrev=False))


def for_lane(r, lane):
    # Every blocker also reaches the lead, whoever it is addressed to.
    return (lane == 'all' or r['to'] in (lane, 'all') or r.get('owner') == lane
            or (lane == 'lead' and r.get('kind') == 'blocker'))
def prs_tag(r):
    tags = f" prs={','.join('#' + p for p in r['prs'])}" if r.get('prs') else ''
    for key in ('state', 'subject', 'needs', 'evidence', 'rollback'):
        if r.get(key): tags += f' {key}={r[key]}'
    if r.get('irreversible'): tags += ' IRREVERSIBLE'
    if r.get('re') and r.get('kind') == 'decision': tags += f" re={r['re']}"
    return tags
def brief(r):
    tag = f" id={r['id']}" if r.get('id') else ''
    return (f"{r['ts'][11:16]}Z {r['frm']} {r['kind']}/{r['topic']}{tag}{prs_tag(r)} "
            f"{' '.join((r.get('text') or '').split())[:120]}")
class ReplayIndex:
    """Runner-owned source identity set, refreshed under the bus append lock.

    The log remains the crash-recovery authority. TailReader reads it once, then
    only newly committed lines, including appends by another process.
    """
    def __init__(self, path):
        from .tail import TailReader
        self.reader = TailReader(path)
        self.seen = set()

    @staticmethod
    def key(row):
        origin, source = row.get('origin'), row.get('_relay')
        if (isinstance(origin, str) and origin.startswith('relay:')
                and isinstance(source, dict) and type(source.get('seq')) is int):
            return origin, source['seq']
        return None

    def sync(self):
        start = (self.reader.state or {}).get('count', 0)
        _, lines = self.reader.read(start)
        if self.reader.reset:
            self.seen.clear()
        for _, line in lines:
            try:
                row = json.loads(line)
            except (ValueError, RecursionError):
                continue
            key = self.key(row) if isinstance(row, dict) else None
            if key is not None:
                self.seen.add(key)


def append(recs, project=True, replay_index=None, on_commit=None):
    """Append rows durably, then (unless project=False) run the ANX projection, which may exit 1.
    Callers that must act after the append whatever ANX does (addressed escalation) pass
    project=False and call project() last. on_commit records an in-memory receipt
    after fsync, before any fallible post-append sinks or projection."""
    CONFIG.writable()
    BUS.parent.mkdir(parents=True, exist_ok=True)
    from .wake import refuse_retired, state_path
    from .addressed import locked
    with locked(state_path(CONFIG).with_suffix('.lock')):
        for rec in recs:
            if not str(rec.get("origin", "")).startswith("relay:"):
                refuse_retired(CONFIG, resolve(rec.get('to', '')))
        with open(BUS, 'a+b') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            from .tail import TailReader
            total, _ = TailReader(BUS, STATE / 'append.offset.json').read(10 ** 18)
            f.seek(0, os.SEEK_END)
            if f.tell():
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b'\n':
                    raise ManifestError('bus_incomplete_tail: repair the partial writer before appending')
            # Refresh the runner's incremental index while holding the same
            # lock as every writer. A restart rebuilds from fsynced log rows.
            if any('_relay' in rec for rec in recs):
                replay_index = replay_index or ReplayIndex(BUS)
                replay_index.sync()
                seen = set()
                fresh = []
                for rec in recs:
                    key = replay_index.key(rec)
                    if key is None or (key not in replay_index.seen and key not in seen):
                        fresh.append(rec)
                        seen.add(key)
                recs = fresh
            for rec in recs:
                rec.update(v=2, seq=total)
                f.write((json.dumps(rec) + '\n').encode())
                total += 1
            f.flush()
            os.fsync(f.fileno())
            if replay_index is not None:
                replay_index.seen.update(key for rec in recs
                                         if (key := replay_index.key(rec)) is not None)
    if on_commit is not None:
        on_commit()
    # Named extension seam: committed rows only, never under the append lock.
    from .adapters import notify
    notify(CONFIG, recs)
    if project:
        project_pending()


def project_pending():
    if NO_MIRROR or not CONFIG.bus.mirror or not CONFIG.anx.workspace:
        return
    # Serialize append + projection with the command lock. Never skip a row that failed for a transient reason; a definite card absence is dead-lettered.
    cursor = CONFIG.paths.root / 'state' / 'mirror.cursor'
    start = int(cursor.read_text()) if cursor.exists() else 0
    all_rows = read()
    for index in range(start, len(all_rows)):
        row = all_rows[index]
        # Project only rows coordkit wrote (origin, or the text_hash every coordkit post carries). On a shared
        # bus (an incumbent tool still writing during a transition) other writers' rows are theirs: skipping them keeps a historic or
        # foreign row from blocking coordkit's own posts.
        ours = not str(row.get('origin', '')).startswith('relay:') and (row.get('origin') == 'coordkit' or 'text_hash' in row)
        from . import addressed  # pickup wakes/alerts are bus-only, never cards
        if ours and not addressed.is_escalation(row) and (row['kind'] in CONFIG.bus.mirror_kinds or row['kind'] == 'ack'):
            try:
                from .adapters.builtin import Anx
                Anx().project(CONFIG, row, all_rows[:index + 1])
            except (ToolError, ManifestError, OSError, ValueError) as exc:
                from . import addressed
                missing = str(getattr(exc, 'detail', '')).split(':', 2)
                handle = missing[1] if len(missing) > 1 and missing[0] == 'card' else ''
                if getattr(exc, 'category', '') == 'card_unresolved' and (handle in addressed.known_names(CONFIG)
                        or (row['kind'] == 'ack' and 'not_found' in str(exc))):
                    # An ack only notes the ruling's card; a missing subject card loses nothing (the ack is on the bus).
                    # A registered bus name is not a card: such rows (acks and asks between agents, coordkit's own
                    # internal asks) live on the bus only. Skip without a dead letter, or the dead-letter report
                    # (itself an ask to that name) would feed itself every reconcile.
                    why = 'is a bus name' if handle in addressed.known_names(CONFIG) else 'does not exist (ack: bus only)'
                    print(f"MIRROR_SKIPPED {row['kind']} {row['id']}: card:{handle} {why}", file=sys.stderr)
                elif getattr(exc, 'category', '') == 'card_unresolved' and 'not_found' in str(exc):
                    # A definite absence: dead-letter this one row
                    # with its reason and keep projecting. Outages still stop and retry.
                    (CONFIG.paths.root / 'state').mkdir(parents=True, exist_ok=True)
                    with open(CONFIG.paths.root / 'state' / 'mirror-dead-letters.jsonl', 'a') as dead:
                        dead.write(json.dumps({'at': now().isoformat(timespec='seconds'), 'id': row['id'],
                                               'kind': row['kind'], 'reason': str(exc)}) + '\n')
                    print(f"MIRROR_DEAD_LETTER {row['kind']} {row['id']} {exc}", file=sys.stderr)
                else:
                    print(f"MIRROR_FAILED {row['kind']} {row['id']} {exc}", file=sys.stderr)
                    raise SystemExit(1) from exc
        STATE.mkdir(parents=True, exist_ok=True)
        tmp = cursor.with_suffix('.tmp')
        tmp.write_text(str(index + 1) + '\n')
        tmp.replace(cursor)


_EXECUTIONS = None
LAST_POST = None  # (row, addressed outcome) of the last post in this process, for `coordkit send`

def aliases(manifest=None):
    manifest = manifest or CONFIG
    parent = manifest.paths.bus_log.parent
    result = dict(manifest.bus.aliases)
    for path in (parent/'aliases.json', parent/'state'/'aliases.json'):
        try:
            doc = read_json(path, {})
        except (OSError, ValueError):
            continue
        values = doc.get('aliases', doc) if isinstance(doc, dict) else {}
        if isinstance(values, dict):
            result.update({k:v for k,v in values.items() if isinstance(v, str) and v})
    return result


def resolve(name, book=None):
    book, seen = (aliases() if book is None else book), set()
    while name in book and name not in seen:
        seen.add(name); name = book[name]
    if name in seen:
        raise ValueError('alias cycle: '+name)
    return name

def known_recipients(rows):
    from .relay import participants
    known = {'lead', 'all'} | {r['frm'] for r in rows} | set(aliases().values())
    known |= set(participants(CONFIG, rows))
    known |= {a.label for a in CONFIG.agents}
    known |= {label for e in agentctl_recent() if e.get('state') in ('running', 'starting')
              for label in e.get('labels') or []}
    return known

def agentctl_recent():
    global _EXECUTIONS
    if not CONFIG.bus.live_check:
        return []
    if _EXECUTIONS is None:
        from .adapters import get
        adapter = get(CONFIG, 'agentctl')
        if adapter is None:
            return []
        executions = adapter.client(CONFIG)
        # Liveness only needs recent history: ended executions older than the bus's live window
        # say nothing that 'no live execution' does not.
        since = (now() - dt.timedelta(hours=6)).strftime('%Y-%m-%dT%H:%M:%SZ')
        by_id = {e['id']: e for e in [*executions.recent(since=since), *executions.recent(nonterminal=True)]}
        _EXECUTIONS = list(by_id.values())
    return _EXECUTIONS
def label_matches(label, to):
    """A recipient owns its exact label and labels it prefixes with '-' or '.' ('worker-a' owns worker-a-task)."""
    return label == to or label.startswith((to + '-', to + '.'))
def launched_at(to):
    """Newest agentctl created_at for executions labelled `to`, or None."""
    times = [parse_ts(e['created_at']) for e in agentctl_recent()
             if any(label_matches(l, to) for l in e.get('labels') or []) and e.get('created_at')]
    return max(times, default=None)
def open_rulings(rows):
    """Rulings that name an owner and have no ack from that owner yet."""
    return [r for i, r in enumerate(rows) if r.get('kind') == 'ruling' and r.get('owner')
            and not any(reply.get('re') == r.get('id') and
                        (reply.get('kind') == 'ack' or (reply.get('kind') == 'answer' and reply.get('frm') == r['owner']))
                        for reply in rows[i + 1:])]
def open_blockers(rows):
    """Blockers with no later decision/answer that names them with --re."""
    closed = {r.get('re') for r in rows if r.get('kind') in BLOCKER_CLOSERS and r.get('re')
              and not r.get('auto_received')}
    return [r for r in rows if r.get('kind') == 'blocker' and r.get('id') not in closed]
def latest_states(rows):
    """{(lane, subject): newest state row}."""
    out = {}
    for r in rows:
        if r.get('kind') == 'state' and r.get('subject'):
            out[(r['frm'], r['subject'])] = r
    return out
def lane_liveness(to, rows):
    """Agentctl labels are authoritative; recent bus activity covers native sessions."""
    if not CONFIG.bus.live_check:
        return None, 'live check disabled'
    if to in live_readers():
        return True, 'reader cursor within 24h'
    executions = agentctl_recent()
    mine = [e for e in executions if any(label_matches(label, to) for label in e.get('labels') or [])]
    if any(e.get('state') in ('running', 'starting') for e in mine):
        return True, 'running agentctl execution'
    last_post = max((parse_ts(r['ts']) for r in rows if r['frm'] == to), default=None)
    if mine:
        ended = max(parse_ts(e.get('updated_at', '1970-01-01T00:00:00Z')) for e in mine)
        if last_post and last_post > ended:
            return True, 'posted after execution ended'
        return False, f"execution is {mine[0].get('state', 'not running')}"
    if last_post and last_post >= now() - dt.timedelta(minutes=LIVE_BUS_MINUTES):
        return True, 'recent bus post'
    return False, 'no live execution or recent bus post'
def live_readers():
    readers = set()
    from urllib.parse import unquote
    for root in (STATE, CONFIG.paths.root / 'state'):
        for path in (root / 'cursors').glob('*'):
            if path.suffix in ('.inbox', '.wait') and time.time() - path.stat().st_mtime < 86400:
                readers.add(unquote(path.stem))
    return readers

def age_minutes(r): return int((now() - parse_ts(r['ts'])).total_seconds() // 60)
def line(r, full=False):
    text = r.get('text') or ''
    if not full:
        return f"{r.get('id', '-')} {r.get('kind', 'msg')} {r['frm']}→{r['to']}: {' '.join(text.split())[:200]}"
    if r.get('ref') or r.get('spill_ref'):
        from .refs import ref_file
        try:
            text = ref_file(CONFIG, r, full=True).read_text().removesuffix('\n')
        except (OSError, UnicodeError, ValueError):
            pass
    return f"{r.get('id', '-')} {r['ts']} {r['frm']}→{r['to']} [{r.get('kind', 'msg')}/{r.get('topic', 'general')}] {text}"

def _main(argv, *, site_local=False, origin='coordkit'):
    from .ops import bus_parser
    p = bus_parser()
    o = p.parse_args(argv)
    o.site_local = site_local
    o.origin = origin
    if o.cmd == 'post':
        targets = o.to if isinstance(o.to, list) else [o.to]
        targets = list(dict.fromkeys(resolve(t) for item in targets for t in item.split(',')))
        if len(targets) > 1:
            from .wake import refuse_retired
            for target in targets:
                refuse_retired(CONFIG, target)
            known = known_recipients(read())
            from .addressed import known_names
            known |= known_names(CONFIG)
            if not o.force and any(t not in known for t in targets):
                raise ManifestError('unknown_recipient: ' + ','.join(t for t in targets if t not in known))
            import copy
            for index, target in enumerate(targets):
                one = copy.copy(o)
                one.to = target
                if o.id: one.id = o.id + '-' + str(index)
                dispatch(one)
            return
        o.to = targets[0]
    if ((o.cmd == 'wait' and o.to and not o.signal and not o.subject)
            or (o.cmd == 'inbox' and o.unread) or (o.cmd == 'harvest' and o.mark)
            or (o.cmd == 'open-rulings' and o.new_only)):
        CONFIG.writable()
        if o.cmd == 'open-rulings':
            lock_path = pathlib.Path(o.new_only).with_name(pathlib.Path(o.new_only).name+'.lock')
        elif o.cmd == 'harvest':
            lock_path = STATE / 'harvest.lock'
        else:
            who = o.to if o.cmd == 'wait' else o.lane
            lock_path = cursor_path(who, o.cmd).with_suffix('.'+o.cmd+'.lock')
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | (fcntl.LOCK_NB if o.cmd == 'wait' else 0))
            except BlockingIOError:
                raise ManifestError('duplicate_waiter: lane cursor already owned') from None
            if o.cmd == 'wait':
                from .adapters.claude_resume import start_time
                from .state import write_json
                write_json(cursor_path(o.to, 'wait').with_suffix('.wait.owner.json'),
                           dict(pid=os.getpid(), started=start_time(os.getpid()),
                                session_id=os.environ.get('CLAUDE_CODE_SESSION_ID', '')))
            return dispatch(o)
    return dispatch(o)


WAIT_RECEIVER_TAIL_BYTES = 1024 * 1024
WAIT_RECEIVER_HEADER = re.compile(r'^(\S+) (?:\S+ \S+→\S+: |\S+ \S+→\S+ \[[^\]\n]+\] )')


def wait_receiver_ids(fd):
    """Recover complete deliveries from a bounded tail of the actual stdout file."""
    # stdout is usually write-only. Reopen for reading rather than seeking or
    # reading its descriptor: /dev/fd on macOS shares the writer's file offset.
    if sys.platform == 'darwin':
        path = os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b'\0', 1)[0])
    else:
        path = f'/proc/self/fd/{fd}'
    with open(path, 'rb') as receiver:
        actual, expected = os.fstat(receiver.fileno()), os.fstat(fd)
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise OSError('wait receiver changed while reopening its log')
        offset = max(0, actual.st_size - WAIT_RECEIVER_TAIL_BYTES)
        receiver.seek(max(0, offset - 1))
        boundary = not offset or receiver.read(1) == b'\n'
        receiver.seek(offset)
        data = receiver.read(WAIT_RECEIVER_TAIL_BYTES)
        rows = data.split(b'\n')[:-1]
    if not boundary:
        rows = rows[1:]
    # Full deliveries may contain prose on subsequent lines. A word matching an
    # ID there is not evidence that the corresponding message was delivered.
    ids = {match[1] for row in rows
           if (match := WAIT_RECEIVER_HEADER.match(row.decode('utf-8', errors='replace')))}
    return ids, bool(data and not data.endswith(b'\n'))


def deliver_wait_hits(hits, full):
    """Recover receiver IDs before appending; always sync before cursor commit."""
    try:
        fd = sys.stdout.fileno()
    except (io.UnsupportedOperation, AttributeError):
        fd = None  # In-memory/test receivers have no kernel durability boundary.
    regular = fd is not None and stat.S_ISREG(os.fstat(fd).st_mode)
    sys.stdout.flush()
    delivered, partial = wait_receiver_ids(fd) if regular else (set(), False)
    for row in hits:
        if row['id'] not in delivered:
            if partial:
                print()  # A torn prior write must not swallow the next record's ID.
                partial = False
            print(line(row, full=full))
            delivered.add(row['id'])
    sys.stdout.flush()
    if regular:
        os.fsync(fd)  # A failed receiver sync must leave the cursor unchanged.


def dispatch(o):
    if o.cmd == 'wait' and o.timeout is None:
        o.timeout = 0 if o.persistent else 240
    if o.cmd == 'reply':
        original = next((r for r in read() if r.get('id') == o.id), None)
        if original is None:
            raise ManifestError('unknown_message: ' + o.id)
        from .ops import bus_parser
        post = bus_parser().parse_args(['post', '--from', o.frm, '--to', original['frm'],
                                       '--topic', original.get('topic', 'general'), '--kind', 'answer',
                                       '--re', o.id, '--force', o.text])
        post.to = original['frm']
        dispatch(post)
        if original.get('kind') in ('ask', 'handoff', 'ruling'):
            dispatch(argparse.Namespace(cmd='ack', id=o.id, frm=o.frm, owner=None, force=False, note='replied'))
        return
    if o.cmd == 'post':
        from .routing import register_activity
        # Validate the recipient so work does not silently reach an unknown lane.
        # Known but silent for IDLE_HOURS -> post it AND copy the lead so it isn't lost.
        rows = read()
        if resolve(o.to) != o.to:
            print(f"note: '{o.to}' is an alias of '{resolve(o.to)}' (manifest aliases); posting to '{resolve(o.to)}'",
                  file=sys.stderr)
            o.to = resolve(o.to)
        from .wake import refuse_retired
        refuse_retired(CONFIG, o.to)
        if o.owner and resolve(o.owner) != o.owner:
            o.owner = resolve(o.owner)
        if o.id:
            old = next((r for r in rows if r.get('id') == o.id), None)
            payload = {k: getattr(o,k) for k in ('frm','to','topic','kind','owner','state','subject','needs',
                                                  'rollback','irreversible','evidence','re','signal')}
            payload['text_hash'] = hashlib.sha256(o.text.encode()).hexdigest()
            payload['prs'] = list(dict.fromkeys(o.pr.replace(' ', ',').split(','))) if o.pr else []
            payload['ref'] = str(pathlib.Path(o.ref).resolve()) if o.ref else None
            signature = hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
            if old and old.get('publication_hash'):
                if old['publication_hash'] != signature:
                    raise ManifestError('publication_id_conflict: '+o.id)
                append([])
                register_activity(CONFIG, o.frm, 'post', env={} if getattr(o, 'origin', None) == 'mcp' else None)
                print(f'already posted id={o.id}')
                return
            if old:
                actual = {k: old.get(k) for k in payload}
                actual['irreversible'] = bool(old.get('irreversible'))
                actual['prs'] = old.get('prs', [])
                actual['text_hash'] = old.get('text_hash', hashlib.sha256(old.get('text','').encode()).hexdigest())
                # Auto-generated spill paths are transport detail, not publication identity.
                if not o.ref and old.get('text_hash') and len(o.text) > TEXT_CAP:
                    actual['ref'] = None
                if actual != payload:
                    raise ManifestError('publication_id_conflict: '+o.id)
                append([])
                register_activity(CONFIG, o.frm, 'post', env={} if getattr(o, 'origin', None) == 'mcp' else None)
                print(f'already posted id={o.id}')
                return
        # Addressed bus (brief 8.2/8.3): a registered name or role is checked against waiter leases, never
        # agentctl, and is never silently copied to the lead; deafness escalates to its declared fallback.
        from . import addressed
        target = addressed.target_kind(CONFIG, o.to)
        role = None
        if target == 'role':
            role, member = o.to, addressed.first_live(CONFIG, o.to)
            if member:
                o.to = member
        if target:
            from .relay import participants
            known = ({'lead', 'all'} | {r['frm'] for r in rows} | set(aliases().values())
                     | {a.label for a in CONFIG.agents} | addressed.known_names(CONFIG)
                     | set(participants(CONFIG, rows)))
        else:
            known = known_recipients(rows)
        if o.to not in known and not o.force:
            print(f"refused: unknown recipient '{o.to}'. Post to 'lead' (or pass --force). Known recent: "
                  + ', '.join(sorted({r['frm'] for r in rows[-200:]})), file=sys.stderr)
            sys.exit(2)
        # A ruling names exactly one owner, who must acknowledge it; shared ownership loses accountability.
        if o.kind == 'blocker':
            # A blocker names the one lane that can clear it and what it needs, like a ruling's owner.
            if not o.owner or not re.fullmatch(r'[A-Za-z0-9_.-]+', o.owner) or o.owner == 'all':
                print("refused: a blocker needs exactly one --owner (the lane that can unblock it; 'lead' is fine)",
                      file=sys.stderr); sys.exit(2)
            if not (o.needs or '').strip():
                print('refused: a blocker needs --needs "<what would unblock it>"', file=sys.stderr); sys.exit(2)
            if o.owner not in known and not o.force:
                print(f"refused: unknown blocker owner '{o.owner}' (or pass --force)", file=sys.stderr); sys.exit(2)
        elif o.needs:
            print('refused: --needs is only for --kind blocker', file=sys.stderr); sys.exit(2)
        if o.kind == 'state':
            if o.state not in STATES:
                print(f"refused: --kind state needs --state {'|'.join(STATES)}", file=sys.stderr); sys.exit(2)
            if not o.subject:
                print('refused: --kind state needs --subject (the train/run/card it is about)', file=sys.stderr)
                sys.exit(2)
        elif o.state:
            print('refused: --state is only for --kind state', file=sys.stderr); sys.exit(2)
        if o.subject and not SUBJECT.match(o.subject):
            print(f'refused: --subject {o.subject!r} must be one token (letters, digits, _.:#/@+-)', file=sys.stderr)
            sys.exit(2)
        if o.kind == 'decision':
            if not (o.re or o.subject):
                print('refused: a decision needs --re <ask/blocker id> or --subject', file=sys.stderr); sys.exit(2)
            if bool((o.rollback or '').strip()) == o.irreversible:
                print('refused: a decision needs exactly one of --rollback "<how to undo>" or --irreversible',
                      file=sys.stderr); sys.exit(2)
        elif o.rollback or o.irreversible:
            print('refused: --rollback/--irreversible are only for --kind decision', file=sys.stderr); sys.exit(2)
        if o.kind == 'handoff' and not o.evidence:
            print('warning: a handoff should carry --evidence <SHA|run id|path>', file=sys.stderr)
        elif o.evidence and o.kind != 'handoff':
            print('refused: --evidence is only for --kind handoff', file=sys.stderr); sys.exit(2)
        if o.kind == 'ruling':
            if not o.owner or not re.fullmatch(r'[A-Za-z0-9_.-]+', o.owner) or o.owner in ('all', 'lead'):
                print("refused: a ruling needs exactly one --owner (a single lane, not 'all'/'lead'). "
                      "Split work between lanes = two rulings, one owner each.", file=sys.stderr)
                sys.exit(2)
            if o.owner not in known and not o.force:
                print(f"refused: unknown ruling owner '{o.owner}' (or pass --force)", file=sys.stderr)
                sys.exit(2)
        elif o.owner and o.kind != 'blocker':
            print('refused: --owner is only for --kind ruling or blocker', file=sys.stderr); sys.exit(2)
        if (o.kind == 'signal') != bool(o.signal):
            print('refused: --kind signal and --signal NAME go together', file=sys.stderr); sys.exit(2)
        if o.signal and not SIGNAL_NAME.match(o.signal):
            print(f"refused: signal name '{o.signal}' must be UPPER_SNAKE (e.g. RAMP5_ON)", file=sys.stderr); sys.exit(2)
        # Structured merge holds avoid interpreting ordinary status prose as a blocking hold.
        # Only explicit hold/unhold rows and authorized lead free text control the gate.
        prs = []
        if o.kind in ('hold', 'unhold'):
            if not o.pr:
                print(f'refused: --kind {o.kind} needs --pr N[,N] naming the PRs it covers', file=sys.stderr)
                sys.exit(2)
            for part in o.pr.replace(' ', ',').split(','):
                if not re.fullmatch(r'\d{3,7}', part):
                    print(f'refused: --pr wants comma-separated PR numbers, got {part!r}', file=sys.stderr)
                    sys.exit(2)
                if part not in prs:
                    prs.append(part)
        elif o.pr:
            print('refused: --pr is only for --kind hold/unhold', file=sys.stderr); sys.exit(2)
        # Reply to the asker so the answer reaches the lane that is waiting.
        if o.re:
            orig = next((r for r in rows if r.get('id') == o.re), None)
            if orig is None:
                print(f"warning: --re {o.re} matches no message id", file=sys.stderr)
            elif orig.get('kind') == 'ask' and o.to not in (orig['frm'], 'all'):
                print(f"warning: {o.re} was an ask from {orig['frm']}; answers go to the asker "
                      f"(you posted to {o.to}). Forward with --kind fyi if someone else owns it.", file=sys.stderr)
        from .relay import remote_only, source_site
        local_reader = o.to not in remote_only(CONFIG, rows) or o.to in live_readers()
        check_live = o.kind != 'fyi' and not target and o.to not in ('lead', 'all') and local_reader
        local_rows = [r for r in rows if not source_site(r)] if CONFIG.relay else rows
        last = max((parse_ts(r['ts']) for r in local_rows if r['frm'] == o.to), default=None)
        launch = launched_at(o.to) if check_live else None
        seen = max((t for t in (last, launch) if t is not None), default=None)
        silent = check_live and (seen is None or now() - seen > dt.timedelta(hours=IDLE_HOURS))
        live, why = lane_liveness(o.to, local_rows) if check_live else (None, '')
        # A recipient with no live agent never reads the post: warn and copy the lead.
        idle = silent or live is False
        if live is False:
            print(f'warning: {o.to} has no live agent ({why}); posting anyway and copying the lead. '
                  'Re-home the work rather than waiting on an idle lane (rule 20).', file=sys.stderr)
        if o.ref and not pathlib.Path(o.ref).is_file():
            print(f'refused: --ref {o.ref} is not a file', file=sys.stderr); sys.exit(2)
        if not o.force and not o.id and o.kind not in ('state', 'fyi'):
            digest = hashlib.sha256(o.text.encode()).hexdigest()
            duplicate = next((r for r in reversed(rows) if r.get('frm') == o.frm and r.get('to') == o.to
                              and r.get('kind') == o.kind and r.get('topic') == o.topic
                              and r.get('re') == o.re and r.get('owner') == o.owner
                              and r.get('subject') == o.subject and r.get('state') == o.state
                              and all(r.get(k) == getattr(o, k) for k in ('needs', 'rollback', 'evidence', 'signal', 'priority'))
                              and bool(r.get('irreversible')) == o.irreversible and r.get('prs', []) == prs
                              and (not o.ref or r.get('ref') == str(pathlib.Path(o.ref).resolve()))
                              and r.get('text_hash', hashlib.sha256(r.get('text', '').encode()).hexdigest()) == digest
                              and now() - parse_ts(r['ts']) < dt.timedelta(minutes=5)), None)
            if duplicate:
                append([])  # also retries any pending projection
                print('DEDUPED ' + duplicate['id'])
                return
        # Latched fyi: an fyi identical to the sender's LAST fyi to this recipient
        # on this topic is a repeat of a latched state. Hold it until the text changes, with one reminder every
        # FYI_REMIND_HOURS; a short time window alone cannot suppress periodic unchanged reports.
        if o.kind == 'fyi' and not o.force:
            last = next((r for r in reversed(rows) if r.get('kind') == 'fyi' and r['frm'] == o.frm and r['to'] == o.to
                         and r.get('topic') == o.topic), None)
            if last and last.get('text_hash', hashlib.sha256(last.get('text', '').encode()).hexdigest()) == hashlib.sha256(o.text.encode()).hexdigest() and \
                    now() - parse_ts(last['ts']) < dt.timedelta(hours=FYI_REMIND_HOURS):
                print(f"deduped: same as your last fyi to {o.to} on {o.topic} (id={last['id']}, {age_minutes(last)} min ago); "
                      f"held until the text changes or {FYI_REMIND_HOURS}h pass (--force posts anyway)")
                return
        # Latched state: only a transition posts. The same state for the same subject from the same lane is
        # held, whatever the text says, with one reminder every FYI_REMIND_HOURS.
        if o.kind == 'state' and not o.force:
            last = latest_states(rows).get((o.frm, o.subject))
            if last and last.get('state') == o.state and \
                    now() - parse_ts(last['ts']) < dt.timedelta(hours=FYI_REMIND_HOURS):
                print(f"deduped: {o.subject} is already {o.state} from {o.frm} (id={last['id']}, "
                      f"{age_minutes(last)} min ago); only a state change posts (--force posts anyway)")
                return
        rid, text = o.id or secrets.token_hex(3), o.text
        spill_ref = None
        tag = (f'{o.to} silent >{IDLE_HOURS}h' if silent else f'{o.to} has no live agent') if idle else ''
        copy_prefix = f'[copy: {tag}] ' if idle else ''
        if len(copy_prefix) >= TEXT_CAP:
            copy_prefix = ''
        room = TEXT_CAP - len(copy_prefix)  # the lead copy must fit the cap too
        if len(text) > room:
            # Token budget: every reader pays for every post. Keep the post short, the detail on disk.
            spill = STATE / 'refs' / f'{now():%m%d}-{rid}.txt'
            spill.parent.mkdir(parents=True, exist_ok=True)
            spill.write_text(text + '\n')
            spill_ref = str(spill)
            suffix = f' … [full: {spill}]'
            if len(suffix) > room:
                suffix = f' … [full: state/refs/{spill.name}]'
            if len(suffix) > room:
                suffix = ''
            text = text[:max(0, room - len(suffix))].rstrip() + suffix
            print(f'note: post text is capped at {TEXT_CAP} chars; the full text is in {spill}. '
                  'Next time write detail to a file and pass --ref PATH.', file=sys.stderr)
        rec = dict(ts=now().isoformat(timespec='seconds'), id=rid, origin=getattr(o, 'origin', 'coordkit'), frm=o.frm, to=o.to,
                   topic=o.topic, kind=o.kind, text=text, text_hash=hashlib.sha256(o.text.encode()).hexdigest())
        if o.id: rec['publication_hash'] = signature
        if o.owner: rec['owner'] = o.owner
        if o.re: rec['re'] = o.re
        if spill_ref:
            rec['ref'] = rec['spill_ref'] = spill_ref
        if o.ref: rec['ref'] = str(pathlib.Path(o.ref).resolve())
        if o.signal: rec['signal'] = o.signal
        if prs: rec['prs'] = prs
        for key in ('state', 'subject', 'needs', 'rollback', 'evidence'):
            if getattr(o, key): rec[key] = getattr(o, key)
        if o.irreversible: rec['irreversible'] = True
        if o.priority: rec['priority'] = o.priority
        if role: rec['role'] = role
        if getattr(o, 'site_local', False): rec['site_local'] = True
        recs = [rec]
        if idle and o.kind not in ('blocker', 'fyi'):  # blockers already reach the lead; FYIs never copy
            recs.append(dict(rec, to='lead', id=secrets.token_hex(3), copy_of=rec['id'], orig_kind=o.kind,
                             text=copy_prefix + text, site_local=True))
            recs[-1].pop('owner', None)  # the copy is not a second ruling to ack
            if o.kind == 'ruling': recs[-1]['kind'] = 'fyi'  # orig_kind still wakes `bus wait --to lead`
        # Addressed escalation runs before the ANX projection: a mirror failure (exit 1) must never
        # stop a deaf/vacant alert from reaching the fallback, relay file or pager.
        global LAST_POST
        def committed():
            global LAST_POST
            LAST_POST = (rec, None)
        append(recs, project=not target, on_commit=committed)
        outcome = None
        try:
            register_activity(CONFIG, o.frm, 'post', env={} if rec['origin'] == 'mcp' else None)
            outcome = addressed.after_post(CONFIG, rec, role) if target else None
        finally:
            # Escalation may itself post. Preserve the caller's durable receipt,
            # including when delivery exits before returning an outcome.
            LAST_POST = (rec, outcome)
        if target:
            project_pending()
        print('posted', rec['ts'], 'id=' + rec['id'] + (f' (recipient {o.to} idle; lead copied)' if len(recs) > 1 else '')
              + (f' — {o.owner} must run: bus ack {rec["id"]} --from {o.owner}' if o.kind == 'ruling' else '')
              + (f' — open until a decision/answer with --re {rec["id"]}' if o.kind == 'blocker' else ''))
    elif o.cmd == 'ack':
        rows = read()
        work = next((r for r in rows if r.get('id') == o.id and r.get('kind') in ('handoff', 'ask')), None)
        if work is not None:
            # pick up (or reassign with --owner) a handoff or ask.
            owner = o.owner or o.frm
            append([dict(ts=now().isoformat(timespec='seconds'), id=secrets.token_hex(3), origin='coordkit', frm=o.frm, to=work['frm'],
                         topic=work.get('topic', 'general'), kind='ack', re=o.id, owner=owner,
                         text=o.note or (f'picked up by {owner}' if owner == o.frm else f'reassigned to {owner}'))])
            from .routing import register_activity
            register_activity(CONFIG, o.frm, 'ack')
            print(f'acked {o.id} owner={owner}')
            return
        ruling = next((r for r in rows if r.get('id') == o.id and r.get('kind') == 'ruling'), None)
        if ruling is None:
            print(f'refused: no ruling with id {o.id}', file=sys.stderr); sys.exit(2)
        if ruling.get('owner') != o.frm and not o.force:
            print(f"refused: ruling {o.id} is owned by {ruling.get('owner')}, not {o.frm}. The owner acks; "
                  "if it is really yours, ask the lead to re-rule (or pass --force).", file=sys.stderr)
            sys.exit(2)
        if any(r.get('kind') == 'ack' and r.get('re') == o.id for r in rows):
            print(f'already acked: {o.id}'); return
        append([dict(ts=now().isoformat(timespec='seconds'), id=secrets.token_hex(3), origin='coordkit', frm=o.frm, to=ruling['frm'],
                     topic=ruling.get('topic', 'general'), kind='ack', re=o.id,
                     text=(o.note or 'ack') + ('' if ruling.get('owner') == o.frm else f' [forced; owner {ruling.get("owner")}]'))])
        from .routing import register_activity
        register_activity(CONFIG, o.frm, 'ack')
        print(f'acked {o.id} ({age_minutes(ruling)} min after the ruling)')
    elif o.cmd == 'open-rulings':
        rows = [r for r in open_rulings(read()) if age_minutes(r) >= o.min_age]
        if o.new_only:
            seen_path = pathlib.Path(o.new_only)
            seen = set(seen_path.read_text().split()) if seen_path.exists() else set()
            rows = [r for r in rows if r['id'] not in seen]
            if rows:
                seen_path.parent.mkdir(parents=True, exist_ok=True)
                with seen_path.open('a') as f: f.write(''.join(r['id'] + '\n' for r in rows))
        if o.json:
            print(json.dumps([dict(r, age_minutes=age_minutes(r)) for r in rows]))
        else:
            for r in rows:
                print(line(r, full=True) if o.full else f"{r['id']} age={age_minutes(r)}m owner={r['owner']} from={r['frm']} [{r['topic']}] {' '.join(r['text'].split())[:200]}")
            if not rows and not o.new_only: print('no open rulings')
    elif o.cmd == 'state':
        rows = [r for (lane, subject), r in sorted(latest_states(read()).items(), key=lambda kv: kv[1]['ts'])
                if (not o.lane or lane == o.lane) and (not o.subject or subject == o.subject)]
        if o.json:
            print(json.dumps([dict(r, age_minutes=age_minutes(r)) for r in rows]))
        else:
            for r in rows:
                print(line(r, full=True) if o.full else f"{r.get('id', '-')} {r['frm']} {r['subject']} {r['state']} age={age_minutes(r)}m "
                      f"{' '.join((r.get('text') or '').split())[:160]}")
            if not rows: print('no state rows')
    elif o.cmd == 'open-blockers':
        rows = [r for r in open_blockers(read()) if not o.owner or r.get('owner') == o.owner]
        if o.json:
            print(json.dumps([dict(r, age_minutes=age_minutes(r)) for r in rows]))
        else:
            for r in rows:
                print(line(r, full=True) if o.full else f"{r['id']} age={age_minutes(r)}m owner={r['owner']} from={r['frm']} needs={' '.join(r.get('needs', '').split())[:80]} "
                      f"[{r['topic']}] {' '.join(r['text'].split())[:200]}")
            if not rows: print('no open blockers')
    elif o.cmd == 'wait' and o.subject:
        # Stateless, like --signal, but judged on the LATEST state per (lane, subject): a DONE that a later RUNNING
        # superseded never satisfies the wait. Match the structured state, never a quoted estimate in prose.
        # Exit 0 on an accepted state, 4 when it
        # ends on FAILED unless FAILED is the only state asked for (`wait && next` never runs after a failure),
        # 3 on timeout.
        accept = {x.strip().upper() for x in (o.state or 'DONE,FAILED').split(',') if x.strip()}
        bad = accept - set(STATES)
        if bad:
            print(f"refused: --state {','.join(sorted(bad))} is not one of {'/'.join(STATES)}", file=sys.stderr)
            sys.exit(2)
        cut = since(o.since) or now()
        delivered = collections.deque(maxlen=4096)
        deadline = time.time() + o.timeout * 60
        while True:
            latest = {}
            for r in read():
                if (r.get('kind') == 'state' and r.get('subject') == o.subject and not r.get('copy_of')
                        and (not o.frm or r['frm'] == o.frm)):
                    latest[r['frm']] = r
            hits = sorted((r for r in latest.values() if r.get('state') in accept
                           and parse_ts(r['ts']) >= cut and r['id'] not in delivered), key=lambda r: r['ts'])
            if hits:
                r = hits[0]
                print(f"BUS_STATE {o.subject} {r['state']} from={r['frm']} ts={r['ts']} id={r.get('id', '')}", flush=True)
                if o.persistent:
                    delivered.append(r['id'])
                else:
                    sys.exit(4 if r['state'] == 'FAILED' and accept != {'FAILED'} else 0)  # only a FAILED-only wait succeeds on it
            if not (o.persistent and o.timeout == 0) and time.time() >= deadline:
                print(f"BUS_WAIT timeout subject={o.subject} states={','.join(sorted(accept))}"); sys.exit(3)
            time.sleep(o.poll)
    elif o.cmd == 'inbox' and o.unread:
        total, indexed = read_from(get_cursor(o.lane, 'inbox', 0))
        cut = since(o.since)
        rows = [r for _, r in indexed if for_lane(r, o.lane) and not (cut and parse_ts(r['ts']) < cut)]
        for r in rows: print(line(r, full=True) if o.full else brief(r) if o.brief else line(r))
        set_cursor(o.lane, 'inbox', total)
    elif o.cmd == 'wait' and o.signal:
        # Stateless: from the end of the log (or --since back), first kind=signal post whose signal field matches.
        cut = since(o.since)
        total, indexed = read_from(0 if cut else 10 ** 12)
        deadline = time.time() + o.timeout * 60
        while True:
            for _, r in indexed:
                if (r.get('kind') == 'signal' and r.get('signal') == o.signal and (not o.frm or r['frm'] == o.frm)
                        and not (cut and parse_ts(r['ts']) < cut)):
                    print(f"BUS_SIGNAL {o.signal} from={r['frm']} ts={r['ts']} id={r.get('id', '')}", flush=True)
                    if not o.persistent:
                        return
            if not (o.persistent and o.timeout == 0) and time.time() >= deadline:
                print(f'BUS_WAIT timeout signal={o.signal}'); sys.exit(3)
            time.sleep(o.poll)
            total, indexed = read_from(total) if total else read_from(0)
    elif o.cmd == 'wait':
        if not o.to:
            print('refused: bus wait needs --to LANE, --signal NAME or --subject SUBJECT', file=sys.stderr); sys.exit(2)
        from .routing import register_activity
        register_activity(CONFIG, o.to, 'wait')
        kinds = {k.strip() for k in o.kinds.split(',') if k.strip()}
        start = get_cursor(o.to, 'wait', None)
        if start is None: start = read_from(10 ** 12)[0]  # first wait: only messages from now on
        deadline = time.time() + o.timeout * 60
        own_asks = collections.deque(maxlen=4096)
        if o.own_asks:
            # Seed only a bounded physical tail; the live set is capped as well.
            from .usage import tail_lines
            rows = [r for _, r in parse_lines(tail_lines(BUS, 1024 * 1024))] if BUS.exists() else []
            own_asks.extend(r['id'] for r in rows if r['frm'] == o.to and r.get('kind') == 'ask')
        while True:
            total, indexed = read_from(start)
            if o.own_asks:
                own_asks.extend(r['id'] for _, r in indexed if r['frm'] == o.to and r.get('kind') == 'ask')
            hits = [(index, r) for index, r in indexed if r['frm'] != o.to and
                    (((r.get('orig_kind') or r.get('kind')) in kinds and for_lane(r, o.to))
                     or r.get('re') in own_asks)]
            for index, row in hits:
                deliver_wait_hits([row], o.full)
                # Limit crash recovery to the last delivery, even for a batch
                # whose output would exceed the bounded receiver tail.
                set_cursor(o.to, 'wait', index + 1)
            set_cursor(o.to, 'wait', total)
            if hits and not o.persistent:
                return
            start = total
            if not (o.persistent and o.timeout == 0) and time.time() >= deadline:
                print(f'BUS_WAIT timeout {o.to} kinds={",".join(sorted(kinds))}'); sys.exit(3)
            time.sleep(o.poll)
    elif o.cmd in ('inbox', 'tail'):
        rows = read(since(getattr(o, 'since', None)))
        if o.cmd == 'inbox': rows = [r for r in rows if for_lane(r, o.lane)]
        else: rows = rows[-o.n:]
        for r in rows: print(line(r, full=True) if o.full else brief(r) if getattr(o, 'brief', False) else line(r))
    elif o.cmd == 'harvest':
        cur = STATE / 'harvest.cursor'
        old = CONFIG.paths.root / 'state' / 'harvest.cursor'
        start = get_position((old, cur), 0)
        total, indexed = read_from(start)
        rows = [r for _, r in indexed if r['kind'] in ('lesson', 'debt')]
        for r in rows: print(line(r, full=o.full))
        if not rows: print('nothing to harvest')
        if o.mark:
            for path in dict.fromkeys((old, cur)):
                replace_text(path, str(total) + '\n')
    else:
        rows = read(since(o.since)); pairs = collections.Counter((r['frm'], r['to']) for r in rows); topics = collections.Counter(r['topic'] for r in rows)
        print(f'{len(rows)} messages'); [print(f'  {a}→{b}: {n}') for (a, b), n in pairs.most_common(20)]
        print('topics:'); [print(f'  {t}: {n}') for t, n in topics.most_common(15)]
        via_lead = sum(n for (a, b), n in pairs.items() if 'lead' in (a, b)); print(f'through lead: {via_lead}/{len(rows)}')

def main(argv=None, manifest=None, *, site_local=False, origin='coordkit'):
    argv = list(argv or [])
    no_mirror = '--no-mirror' in argv
    argv = [arg for arg in argv if arg != '--no-mirror']
    configure(manifest, no_mirror)
    command = argv[0] if argv else ''
    tokens = [arg.partition('=')[0] for arg in argv]
    if manifest.mode == 'shadow' and (command in ('post', 'reply', 'ack') or '--unread' in argv
                                      or '--mark' in tokens or '--new-only' in tokens
                                      or (command == 'wait' and '--to' in tokens)):
        manifest.writable()
    if command in ('post', 'reply', 'ack'):
        manifest.writable()
        STATE.mkdir(parents=True, exist_ok=True)
        with (STATE / 'bus.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return _main(argv, site_local=site_local, origin=origin)
    return _main(argv, site_local=site_local, origin=origin)
