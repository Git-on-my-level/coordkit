"""Questions never stall silently.

Modal prompts, prose decisions without a listening waiter and agent-to-agent asks need durable
capture and a path to an authorized answer. Every captured question becomes a record and climbs one ladder:

- tier 1: a LIVE agent with authority (the asker's declared fallback, else `[questions].fallback_role`),
  resolved through the addressed bus (`addressed.resolve_live`), never a dead name;
- tier 2: an `anx ask` with the asker's recommendation, when the question is guarded (merge/ship/deploy/
  delete/credentials/spend/host-or-fleet change, prod user data, product semantics), when no live tier-1
  member exists, or when tier 1 misses the deadline (urgent 15m, normal 30m);

Capture paths: a PreToolUse hook on AskUserQuestion (denies the modal only after the bus ask is posted);
a Stop hook for a prose decision question without listening evidence; stored permission notices; and the
transcript sweep for a pending AskUserQuestion no hook caught. Every hook fails open: any error or a
shadow-mode manifest leaves the tool call untouched. `COORDKIT_QUESTION_GUARD=off` disables all hooks.
"""
import argparse
import datetime as dt
import glob
import hashlib
import json
import math
import os
import re
import secrets
import stat
import subprocess
import sys
import time
from pathlib import Path

from . import addressed
from .manifest import ManifestError, duration
from .process import parse_ts
from .state import read_json, write_json

MAX_STORE_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 10000
MAX_BUS_BYTES = 256 * 1024

STATES = ('open', 'escalated', 'answered', 'closed')
BUS_NAME = re.compile(r'bus name is\W{0,4}([A-Za-z0-9][A-Za-z0-9_.-]{1,63})', re.I)
PROSE = re.compile(r'\b(decision for you|your call|may i\b|should i\b|do you want me to|shall i\b|'
                   r'waiting (on|for) (your)|needs? your (go|approval|decision)|approve\?)', re.I)


def cfg(manifest):
    return manifest.questions


def home(manifest):
    return manifest.paths.root/'state'/'questions'


def now():
    return time.time()


def iso(at):
    return dt.datetime.fromtimestamp(at, dt.timezone.utc).isoformat(timespec='seconds')


# -- store ------------------------------------------------------------------------------------------

def load_all(manifest):
    try:
        fd = os.open(home(manifest)/'questions.json', os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_STORE_BYTES:
                raise ManifestError('question_store_limit: preserve and archive the question store before retrying')
            raw = stream.read(MAX_STORE_BYTES + 1)
        if len(raw) > MAX_STORE_BYTES:
            raise ManifestError('question_store_limit: question store grew beyond byte cap')
        records = json.loads(raw) or {}
    except (FileNotFoundError, json.JSONDecodeError, UnicodeError):
        return {}
    if not isinstance(records, dict):
        return {}
    if len(records) > MAX_RECORDS:
        raise ManifestError('question_store_limit: too many question records; preserve and archive before retrying')
    return {qid: r for qid, r in records.items() if isinstance(r, dict)
            and r.get('qid') == qid and r.get('state') in STATES
            and type(r.get('created_at')) in (int, float) and math.isfinite(r['created_at'])
            and type(r.get('reasks', 0)) is int and r.get('reasks', 0) >= 0
            and type(r.get('closed_at', 0)) in (int, float)
            and (r.get('default') is None or isinstance(r['default'], str))}


CLOSED_RETENTION = 7*86400


def save_all(manifest, records):
    # Closed questions are kept a week for audit, then pruned; open ones stay until answered.
    cutoff = now() - CLOSED_RETENTION
    for qid in [q for q, r in records.items() if r.get('state') == 'closed' and r.get('closed_at', 0) < cutoff]:
        del records[qid]
    if len(records) > MAX_RECORDS or len(json.dumps(records).encode()) + 1 > MAX_STORE_BYTES:
        raise ManifestError('question_store_limit: update would exceed the question store cap')
    write_json(home(manifest)/'questions.json', records)


def update(manifest, fn):
    """Run fn(records) under the store lock and persist; returns fn's result."""
    with addressed.locked(home(manifest)/'questions.lock'):
        records = load_all(manifest)
        result = fn(records)
        save_all(manifest, records)
        return result


TRANSIENT = re.compile(
    r'(?P<protected>\#\d+|\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b)'
    r'|(?P<counter>\b(?P<counter_name>(?i:retry|attempt))'
    r'(?:\s+(?:(?i:count|number)\s*)?[:=]?\s*|[:=]\s*)\d+\b)'
    r'|(?P<age>\b(?i:age|aged|oldest)\s*[:=]?\s*\d+(?:\.\d+)?[smhd]\b|\b\d+[mhd]\s+(?i:old)\b)'
    r'|(?P<qid>\b[qQ]-[0-9a-fA-F]{6}\b)'
    r'|(?P<clock>\b\d{4}-\d{2}-\d{2}[Tt ]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:[Zz]|[+-]\d{2}:\d{2})?\b)')


def normalize(text):
    """Strip marked transient metadata, retaining numeric decisions and PR/flag identities."""
    def replace(match):
        if match.group('protected'):
            return match.group()
        return match.group('counter_name') or ''
    return ' '.join(TRANSIENT.sub(replace, text).casefold().split())


def ref_identity(path):
    """Authored ref identity, captured at publication; never confuse it with an automatic spill."""
    if not path:
        return None
    path = Path(path).resolve()
    info = path.stat()
    return dict(path=str(path), device=info.st_dev, inode=info.st_ino,
                size=info.st_size, mtime_ns=info.st_mtime_ns, ctime_ns=info.st_ctime_ns)


def fingerprint(session, text, options=(), recommendation='', default=''):
    """Same session, same question, same choices: a modal repeated with other options is a new question."""
    body = json.dumps([session, normalize(text), list(options), recommendation, default])
    return hashlib.sha256(body.encode()).hexdigest()[:20]


def ask_key(row):
    """Stable across whitespace/case changes and transport spill paths."""
    parts = [str(row.get(k) or '') for k in ('frm', 'to', 'topic')]
    parts[1] = str(row.get('role') or parts[1])
    if isinstance(row.get('key'), str) and row['key'].strip():
        scoped = [p.casefold() for p in parts] + [row['key'].strip()]
        return 'key:' + hashlib.sha256(json.dumps(scoped).encode()).hexdigest()
    parts.append(row['normalized_text_hash'] if isinstance(row.get('normalized_text_hash'), str)
                 else normalized_hash(row.get('text')))
    return hashlib.sha256(json.dumps([p.casefold() for p in parts]).encode()).hexdigest()


def normalized_hash(text):
    return hashlib.sha256(normalize(str(text or '')).encode()).hexdigest()


def recent_rows(manifest, *, stream=None, coverage=False):
    """One shared bounded snapshot for sweeps, capture fallback and the locked default guard."""
    from . import bus
    from .tail import read_tail
    if stream is None:
        try:
            with manifest.paths.bus_log.open('rb') as source:
                return recent_rows(manifest, stream=source, coverage=coverage)
        except FileNotFoundError:
            return (0, []) if coverage else []
    start, data = read_tail(stream, MAX_BUS_BYTES)
    lines = data.split(b'\n')[:-1]
    # A bounded prefix count establishes physical IDs without scanning arbitrary
    # history. If it cannot, omit ID-less rows rather than inventing identities.
    line_offset = None
    if start <= MAX_BUS_BYTES:
        try:
            stream.seek(0)
            prefix = stream.read(start)
            if len(prefix) == start:
                line_offset = prefix.count(b'\n')
        except OSError:
            pass
    if len(lines) > MAX_RECORDS:
        omitted = len(lines) - MAX_RECORDS
        start += sum(len(line) + 1 for line in lines[:omitted])
        if line_offset is not None:
            line_offset += omitted
        lines = lines[-MAX_RECORDS:]
    rows = [row for _, row in bus.parse_lines(
        [line.decode('utf-8', errors='replace') for line in lines], line_offset=line_offset)]
    return (start, rows) if coverage else rows


def bus_record(row, at):
    return dict(qid=row['id'], state='open', tier=0, capture='bus', asker=row['frm'],
                target=row['to'], topic=row.get('topic', 'general'), question=row.get('text', ''),
                created_at=at, fingerprint=ask_key(row), reasks=0, default=row.get('default'),
                owner=row.get('owner'), priority=row.get('priority') or 'normal',
                ask_ref=row.get('ask_ref', {'path': row['ref'], 'unverified': True} if row.get('ref') and row.get('ref') != row.get('spill_ref') else None),
                no_auto_default=row.get('no_auto_default', False),
                info_hash=row.get('normalized_text_hash') or normalized_hash(row.get('text')))


def spill_row(manifest, row):
    """Use the normal bus ref contract for generated rows as well as CLI posts."""
    text, cap = row['text'], manifest.bus.text_cap
    if len(text) > cap:
        path = manifest.paths.bus_log.parent/'state'/'refs'/('coord-'+hashlib.sha256(row['id'].encode()).hexdigest()[:20]+'.txt')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + '\n')
        row.update(ref=str(path), spill_ref=str(path), text_hash=hashlib.sha256(text.encode()).hexdigest(),
                   text=text[:max(0, cap-9)] + ' … [full]')
    return row


def track_bus_ask(manifest, row):
    """Project only a committed original ask; copies are delivery, never new questions."""
    if row.get('kind') != 'ask' or row.get('copy_of') or row.get('question') or row.get('topic') == 'coordkit-digest':
        return
    def add(records):
        record = bus_record(row, parse_ts(row['ts']).timestamp())
        for old in records.values():
            if old.get('capture') == 'bus' and old.get('fingerprint') == record['fingerprint'] \
                    and old['state'] in ('open', 'escalated') and old['qid'] != record['qid']:
                old.update(state='closed', closed_at=record['created_at'], closed_by='superseded')
        records.setdefault(record['qid'], record)
    update(manifest, add)


def track_bus_reply(manifest, row):
    """Persist bus answers before they age out of the bounded reconciliation tail."""
    if os.environ.get('COORDKIT_QUESTION_GUARD') == 'off':
        return
    def apply(records):
        for record in records.values():
            if record['state'] not in ('open', 'escalated'):
                continue
            answer = replies(manifest, [row], record)
            if not answer:
                continue
            if record.get('capture') == 'bus':
                settle(manifest, record, [answer], parse_ts(answer['ts']).timestamp())
            else:
                # The ladder still owns delivery and ANX withdrawal. Save only
                # the first answer, including replies to prior escalation hops.
                record.setdefault('bus_answer', {k: answer[k] for k in
                                  ('id', 'ts', 'seq', 'frm', 're', 'kind', 'text') if k in answer})
    update(manifest, apply)


def default_due(manifest, record, at):
    """One policy: authored re-asks or real ladder windows, plus the bus age limit."""
    cycles = max(record.get('reasks', 0), record.get('cycles', 0))
    return (os.environ.get('COORDKIT_QUESTION_GUARD') != 'off' and not record.get('no_auto_default') and
            (cycles >= cfg(manifest).auto_default_reasks or
             (record.get('capture') == 'bus' and
              at - record['created_at'] >= duration(cfg(manifest).auto_default_after))))


def settlement_rows(manifest, stream, record):
    """Advance a durable no-answer proof, at most one bounded chunk per call.

    Checkpoints cover the entire physical log prefix, including malformed rows,
    rather than a moving tail. The final call runs under the append flock. A
    replaced/truncated log or changed boundary invalidates the proof and restarts
    from zero. Never skip an oversized or incomplete physical row.
    """
    from . import bus
    info = os.fstat(stream.fileno())
    identity = [info.st_dev, info.st_ino]
    prior = record.get('settlement_scan', {})
    prior = prior if isinstance(prior, dict) else {}
    offset, count = prior.get('offset'), prior.get('count')
    valid = (prior.get('file') == identity and type(offset) is int and type(count) is int
             and 0 <= offset <= info.st_size and count >= 0
             and prior.get('no_answer_through') == offset)
    if valid:
        stream.seek(max(0, offset - 128))
        valid = hashlib.sha256(stream.read(min(offset, 128))).hexdigest() == prior.get('boundary')
    if not valid:
        prior, offset, count = {}, 0, 0
    stream.seek(offset)
    data = stream.read(MAX_BUS_BYTES)
    lines = data.split(b'\n')[:-1][:MAX_RECORDS]
    consumed = sum(len(line) + 1 for line in lines)
    fresh = [row for _, row in bus.parse_lines(
        [line.decode('utf-8', errors='replace') for line in lines], line_offset=count)]
    offset += consumed
    stream.seek(max(0, offset - 128))
    checkpoint = dict(file=identity, offset=offset, count=count + len(lines),
                      boundary=hashlib.sha256(stream.read(min(offset, 128))).hexdigest(),
                      original_seen=bool(prior.get('original_seen')) or any(
                          r.get('id') in (record['qid'], record.get('bus_id')) for r in fresh),
                      default_seen=bool(prior.get('default_seen')) or any(
                          r.get('id') == ('auto-' if record.get('capture') == 'bus' else 'qd-') + record['qid']
                          for r in fresh),
                      last_row_id=next((r.get('id') for r in reversed(fresh)), prior.get('last_row_id')))
    # Persist only an actually checked prefix; an answer ends settlement instead.
    if not replies(manifest, fresh, record):
        checkpoint['no_answer_through'] = offset
    record['settlement_scan'] = checkpoint
    complete = offset == info.st_size
    if not complete:
        reason = ('bus row exceeds settlement byte limit' if not consumed and len(data) == MAX_BUS_BYTES else
                  'incomplete bus row' if not consumed else 'bus answer scan catching up')
        record['default_deferred'] = f'{reason} (checked through byte {offset} of {info.st_size})'
    elif record.get('capture') == 'bus' and not checkpoint['original_seen']:
        record['default_deferred'] = 'original ask missing from bus history'
        complete = False
    else:
        record.pop('default_deferred', None)
    return fresh, complete


def settle(manifest, record, rows, at):
    """Under the question lock: one receipt-first default publisher for bus and ladder."""
    if os.environ.get('COORDKIT_QUESTION_GUARD') == 'off' or record['state'] not in ('open', 'escalated'):
        return
    is_bus = record.get('capture') == 'bus'
    answer_id = ('auto-' if is_bus else 'qd-') + record['qid']
    due = default_due(manifest, record, at)
    # Advance even before the deadline, and find old answers outside the tail.
    if is_bus and (not due or not (record.get('default') or '').strip() or manifest.mode != 'live'):
        try:
            with manifest.paths.bus_log.open('rb') as stream:
                fresh, _ = settlement_rows(manifest, stream, record)
            rows = [*rows, *fresh]
        except OSError as exc:
            record['default_deferred'] = f'bus answer scan unavailable: {exc.strerror or type(exc).__name__}'
    # An actual answer always wins, including at the default deadline.
    answer = replies(manifest, rows, record) if is_bus else None
    if answer:
        record.pop('default_deferred', None)
        record.pop('auto_default_applied', None)
        record.update(state='closed', closed_at=at, answered_at=at, answered_by=answer.get('frm'),
                      answer=answer.get('text', ''), answer_id=answer.get('id'))
        return
    if not due:
        return
    if not (record.get('default') or '').strip():
        record['unanswered_no_default'] = True
        return
    if manifest.mode != 'live':
        return
    from . import bus
    if bus.CONFIG is not manifest:
        bus.configure(manifest)
    text = (f'auto-default applied after {record.get("reasks", 0)} unanswered re-asks: {record["default"]}'
            if is_bus else record['default'])
    row = dict(ts=iso(at), id=answer_id, frm='coordkit', to=record['asker'],
               topic=record.get('topic', 'question'), kind='answer' if is_bus else 'decision',
               re=record.get('bus_id') or record['qid'], text=text, rollback=DEFAULT_ROLLBACK,
               auto_default=True)
    receipted = False
    def unanswered(stream):
        nonlocal receipted
        # Q lock -> wake lock -> bus flock, the same order as normal publication.
        # Answer callbacks run after releasing the bus flock, so they may be waiting
        # on our Q lock even though their real answer is already durable.
        if os.environ.get('COORDKIT_QUESTION_GUARD') == 'off':
            return False
        try:
            fresh, complete = settlement_rows(manifest, stream, record)
        except OSError as exc:
            record['default_deferred'] = f'bus answer scan unavailable: {exc.strerror or type(exc).__name__}'
            return False
        answer = replies(manifest, [*rows, *fresh], record)
        if answer:
            record.pop('default_deferred', None)
            record.pop('auto_default_applied', None)
            record.update(state='closed' if is_bus else 'answered', closed_at=at, answered_at=at,
                          answered_by=answer.get('frm'), answer=answer.get('text', ''), answer_id=answer.get('id'))
            return False
        if os.environ.get('COORDKIT_QUESTION_GUARD') == 'off':
            return False
        if not complete:
            return False
        receipted = record['settlement_scan']['default_seen']
        if receipted:
            return False
        # This verified physical-line count also repairs a missing append hint,
        # under the same flock, without a second scan of the historical prefix.
        from . import state
        checkpoint = record['settlement_scan']
        state.write_json(bus.STATE/'append.offset.json', {k: checkpoint[k] for k in ('file', 'offset', 'count', 'boundary')})
        record.pop('default_deferred', None)
        spill_row(manifest, row)
        return True
    try:
        appended = bus.append([row], project=False, before_append=unanswered, max_scan_bytes=MAX_BUS_BYTES)
    except OSError as exc:
        record['default_deferred'] = f'bus default publication unavailable: {exc.strerror or type(exc).__name__}'
        return
    if not appended and not receipted:
        if record['state'] in ('open', 'escalated') and os.environ.get('COORDKIT_QUESTION_GUARD') != 'off':
            record.setdefault('default_deferred', 'bus sequence recovery exceeds bounded scan')
        return
    record.update(answer_id=answer_id, auto_default_applied=True)
    if is_bus:
        record.update(state='closed', closed_at=at, answered_at=at, answered_by='coordkit', answer=text)


def repeat_bus_ask(manifest, row, rows, at=None, *, records=None):
    """Return the original receipt for unchanged information, including exhausted asks.

    The CLI writer lock serializes matching and publication; question.lock also protects sweeps.
    Reuse the caller's rows, including legacy asks not yet in the question projection.
    """
    at = now() if at is None else at
    rows = rows[-10000:]
    key = ask_key(row)
    info = row['normalized_text_hash'] if isinstance(row.get('normalized_text_hash'), str) else normalized_hash(row.get('text'))
    def repeat(records):
        matches = [r for r in records.values() if r.get('capture') == 'bus' and r.get('fingerprint') == key]
        if not matches:
            for old in reversed(rows[-10000:]):
                if old.get('kind') == 'ask' and not old.get('copy_of') and not old.get('question') and ask_key(old) == key:
                    try:
                        record = bus_record(old, parse_ts(old['ts']).timestamp())
                    except (KeyError, TypeError, ValueError, OverflowError):
                        continue
                    records[record['qid']] = record
                    matches = [record]
                    break
        if not matches:
            return None
        record = max(reversed(matches), key=lambda r: r['created_at'])
        if 'ask_ref' not in record or 'priority' not in record:
            return None  # legacy projections cannot prove the authored payload is unchanged
        if (record.get('info_hash') != info or record.get('default') != row.get('default')
                or record.get('owner') != row.get('owner')
                or record.get('ask_ref') != row.get('ask_ref')
                or (record.get('priority') or 'normal') != (row.get('priority') or 'normal')
                or bool(record.get('no_auto_default')) != bool(row.get('no_auto_default'))):
            return None  # an explicit key may carry genuinely new information
        settle(manifest, record, rows, at)
        if record['state'] == 'closed' and not record.get('auto_default_applied'):
            return None  # a fresh ask after a human answer is a new decision
        record['reasks'] = record.get('reasks', 0) + 1
        record['last_seen'] = at
        settle(manifest, record, rows, at)
        return dict(record)
    return repeat(records) if records is not None else update(manifest, repeat)


def reconcile_defaults(manifest, rows, at=None):
    """Advance durable answer scans; due defaults finish under the append lock."""
    if os.environ.get('COORDKIT_QUESTION_GUARD') == 'off':
        return
    at = now() if at is None else at
    by_re = {}
    for row in rows[-10000:]:
        if isinstance(row, dict) and isinstance(row.get('re'), str):
            by_re.setdefault(row['re'], []).append(row)
    def reconcile(records):
        for record in records.values():
            if record.get('capture') == 'bus':
                settle(manifest, record, by_re.get(record['qid'], []), at)
    update(manifest, reconcile)


def queue_escalation(manifest, to, topic, text, *, key=None, default=None, no_auto_default=False, event=None):
    """Digest-bound escalations have a bus receipt without an individual wake."""
    from . import bus
    if bus.CONFIG is not manifest:
        bus.configure(manifest)
    row = dict(ts=iso(now()), id='esc-'+secrets.token_hex(6), frm='coordkit', to=to, topic=topic,
               kind='ask', text=text, digest_only=True, key=key, default=default,
               no_auto_default=no_auto_default, normalized_text_hash=normalized_hash(text))
    rows = recent_rows(manifest)
    def publish(records):
        duplicate = repeat_bus_ask(manifest, row, rows, records=records)
        if duplicate:
            return duplicate['qid'], False
        bus.append([spill_row(manifest, row)], project=False)
        records[row['id']] = bus_record(row, now())
        return row['id'], True
    qid, fresh = update(manifest, publish)
    if fresh:
        addressed.to_lead_digest(manifest, event or topic, text, row)
    return qid


# -- classification ---------------------------------------------------------------------------------

ACTION = re.compile(r'\b(may i|should i|can i|could i|shall i|ok(ay)? to|okay if|approve|go ahead|proceed|'
                    r'do you want me to|want me to|permission to|allowed to|green ?light|sign off|let me)\b'
                    # A question that opens with the action itself: "Delete the bucket?", "Ship #1 now?"
                    r'|(^|[|:]\s*|\n)\W*(merge|ship|deploy|delete|drop|switch|load|unload|install|restart|reboot|'
                    r'roll( ?back| ?out)?|ramp|spend|rotate|revoke|grant|disable|enable)\b', re.I)


READ_ONLY = re.compile(r'\b(may|should|can|could|shall) i\s+(just\s+)?(check|look( at)?|read|view|see|show|list|'
                       r'inspect|query|ask|review|confirm|summari[sz]e|report|watch|monitor)\b', re.I)
HOST_MOVE = re.compile(r'\b(m\d+-[a-z0-9-]+|mac[- ]?studio|mac[- ]?mini|macbook)\b|\b(move|migrate|reassign|relocate|'
                       r'transfer)\b.{0,40}\b(host|machine|worker|server|node)\b', re.I)


PRONOUN = re.compile(r'\b(it|them|this|that|those|these)\b', re.I)
DESCRIBES = re.compile(r'^\W*(it|this|that)(\s+\w+){0,2}\s+(is|are|will|would|means|requires|involves|needs|touches)\b', re.I)


def guarded(manifest, text):
    """(True, why) when only the operator can answer. Each question of a modal (and each paragraph) is a unit; a
    unit is guarded when one of its sentences asks to act (ACTION, not a read-only check) and the unit names
    a guarded object anywhere ("May I run the rehearsal? It is a host change"). A read-only sentence never
    hides a guarded one. `[human-only]` always guards."""
    if any(marker in text.lower() for marker in ('[human-only]', '[da' 'vid-only]')):
        return True, 'guarded (marked human-only)'
    def guarded_object(fragment):
        host = HOST_MOVE.search(fragment)
        if host:
            return f'host or fleet change: {host.group(0).strip()}'
        for pattern in cfg(manifest).guarded_patterns:
            match = re.search(pattern, fragment, re.I)
            if match:
                return match.group(0).strip()
        return None

    for unit in re.split(r'\s\|\s|\n{2,}', text):
        sentences = [x for x in re.split(r'(?<=[?.!])\s+|\n', unit) if x.strip()]
        # A descriptive sentence ("It is a host change", "This means a deploy") qualifies the unit's action.
        described = [guarded_object(x) for x in sentences if DESCRIBES.match(x)]
        described = next((d for d in described if d), None)
        for sentence in sentences:
            clauses = [c for c in re.split(r'\s*(?:,|;|\band then\b|\bthen\b|\band\b)\s*', sentence) if c.strip()]
            for i, clause in enumerate(clauses):
                if not ACTION.search(clause) or READ_ONLY.search(clause):
                    continue
                # The object must belong to this action: in the clause itself, carried by a pronoun from the
                # previous clause ("check the API key and rotate it"), or named by a descriptive sentence.
                found = guarded_object(clause)
                if not found and i and PRONOUN.search(clause):
                    found = guarded_object(clauses[i-1])
                found = found or described
                if found:
                    return True, f'guarded ({found})'
    return False, ''


def from_tool_input(tool_input):
    """(question, options, recommendation) from an AskUserQuestion input."""
    questions = (tool_input or {}).get('questions') or []
    parts, options, recommended = [], [], ''
    for q in questions:
        parts.append((q.get('header') + ': ' if q.get('header') else '') + (q.get('question') or ''))
        for option in q.get('options') or []:
            label = option.get('label') or ''
            options.append(label)
            if not recommended and '(recommended)' in label.lower():
                recommended = label
    return ' | '.join(p for p in parts if p).strip(), options, recommended


# -- identity ---------------------------------------------------------------------------------------

from .identity import asker_for


# -- create and route -------------------------------------------------------------------------------

def create(manifest, asker, question, *, options=(), recommendation='', session_id='', capture='pretooluse',
           urgent=False, config_dir='', remote='', at=None, brief=None, default=None, no_auto_default=False, key=None,
           bus_id=None, bus_target=None):
    """A new open record (or the existing open one for the same session+question). Routes it.
    (None, False) for a session that must never be captured."""
    at = now() if at is None else at
    if session_id in cfg(manifest).exclude_sessions or asker in cfg(manifest).human_sessions:
        return None, False
    default = default.strip() if default is not None else None
    info = fingerprint(session_id or asker, question, tuple(options), recommendation, default or '')
    fp = key or info
    is_guarded, why = guarded(manifest, ' '.join([question, *options]))

    def add(records):
        for record in reversed(list(records.values())):
            if record.get('capture') == 'bus':
                continue
            existing = fingerprint(record.get('session_id') or record['asker'], record['question'],
                                   record.get('options', ()), record.get('recommendation', ''), record.get('default') or '')
            if ((record.get('fingerprint') == fp or (not key and existing == info))
                    and record.get('info_hash', existing) == info
                    and bool(record.get('urgent')) == urgent
                    and record.get('default') == default and bool(record.get('no_auto_default')) == no_auto_default
                    and (record['state'] in ('open', 'escalated', 'answered') or record.get('auto_default_applied'))):
                record.update(fingerprint=fp, info_hash=info, last_seen=at)
                # A transcript sweep observes the same tool call; only another authored invocation is a re-ask.
                if capture not in ('sweep', 'notification') and record['state'] != 'answered':
                    record['reasks'] = record.get('reasks', 0) + 1
                return record, False
        qid = 'q-' + secrets.token_hex(3)
        level = 'urgent' if urgent else 'normal'
        record = {'qid': qid, 'state': 'open', 'question': question[:500], 'options': list(options)[:10],
                  'recommendation': recommendation, 'default': default, 'cycles': 0, 'bus_id': bus_id, 'bus_target': bus_target,
                  'asker': asker, 'session_id': session_id,
                  'config_dir': config_dir, 'remote': remote, 'capture': capture, 'created_at': at,
                  'deadline_at': at + duration(cfg(manifest).urgent_deadline if urgent else cfg(manifest).normal_deadline),
                  'urgent': urgent, 'priority': level, 'guarded': is_guarded, 'guarded_why': why, 'tier': 0,
                  'target': None, 'anx_ask_id': None, 'fingerprint': fp, 'paged': False}
        record.update(default=default, no_auto_default=no_auto_default, reasks=0, info_hash=info)
        for old in records.values():
            if old.get('fingerprint') == fp and old['state'] in ('open', 'escalated'):
                old.update(state='closed', closed_at=at, closed_by='superseded')
        if brief:  # filed by the asker with its question; the asker stays the captured one
            from . import briefs
            record['brief'] = briefs.clean(dict(brief, asker=asker, actor=brief.get('actor') or asker,
                                                details=(brief.get('details') or '') + f'\nquestion {qid}'))
        records[qid] = record
        return record, True
    record, fresh = update(manifest, add)
    if fresh:
        route(manifest, record, at)
    else:
        rows = recent_rows(manifest)
        apply_default(manifest, record, at, rows)
    return load_all(manifest)[record['qid']], fresh


def remote_of(how):
    return how.split(':', 1)[1] if how.startswith('bound:') else ''


def tier1_target(manifest, asker):
    spec = addressed.registry(manifest).get(asker, {})
    # An unknown asker goes to the relay (a human-facing reader), never to a role it cannot answer for.
    chain = (addressed.cfg(manifest).relay,) if asker.startswith('unknown-') else \
        (spec.get('fallback'), cfg(manifest).fallback_role)
    for candidate in chain:
        live = addressed.resolve_live(manifest, candidate, skip=(asker,)) if candidate else None
        if live:
            return live
    return None


def summary(record):
    rec = f' rec: {record["recommendation"]}' if record.get('recommendation') else ''
    opts = f' options: {" / ".join(record["options"][:4])}' if record.get('options') else ''
    default = f' default: {record["default"]}' if record.get('default') else ''
    reasked = f' re-asked {record["reasks"]}x' if record.get('reasks') else ''
    deferred = f' default deferred: {record["default_deferred"]}' if record.get('default_deferred') else ''
    return f'Q {record["qid"]} from {record["asker"]}: {record["question"][:220]}{rec}{opts}{default}{reasked}{deferred}'


def route(manifest, record, at=None):
    """First hop: tier 1 to a live agent with authority, or straight to tier 2."""
    at = now() if at is None else at
    if record.get('bus_id') and not record['guarded']:
        mark(manifest, record['qid'], state='escalated', tier=1, target=record.get('bus_target'), escalated_at=at)
        return 1
    if record['guarded']:
        return tier2(manifest, record, f'{record["guarded_why"]}: a lead cannot approve it', at)
    target = tier1_target(manifest, record['asker'])
    if not target:
        return tier2(manifest, record, 'no live member with authority', at)
    deadline = iso(record['deadline_at'])[11:16] + 'Z'
    text = f'{summary(record)} -- answer by {deadline}: coordkit question answer {record["qid"]} --as {target} "..."'
    post(manifest, record['asker'], target, 'ask', text, record, bus_id=record['qid'], tier=1)
    mark(manifest, record['qid'], state='escalated', tier=1, target=target, escalated_at=at)
    return 1


def post(manifest, frm, to, kind, text, record, bus_id=None, tier=None, re_id=None):
    """A bus row (live mode). In shadow mode the relay file carries it instead."""
    from . import bus
    if manifest.mode != 'live':
        addressed.to_relay_file(manifest, f'QUESTION tier={tier} {text}')
        return None
    bus.configure(manifest)
    row = {'ts': iso(now()), 'id': bus_id or ('qe-' + secrets.token_hex(3)), 'frm': frm, 'to': to,
           'topic': 'question', 'kind': kind, 'text': text[:manifest.bus.text_cap],
           'priority': record.get('priority', 'normal'), 'question': record['qid']}
    if tier is not None and bus_id is None:
        row['escalation'] = f'question_escalated tier={tier}'
    if re_id:
        row['re'] = re_id
    if kind == 'ask' and bus_id == record['qid'] and record.get('default'):
        row['default'] = record['default']
    bus.append([row], project=False)
    return row['id']


def outage_fallback(manifest, record, why, at):
    """anx is down: notify a live agent and the relay once; retry anx every 5 minutes."""
    reached = []
    target = tier1_target(manifest, record['asker'])  # re-resolved now: a live listener only
    if target and addressed.listening(manifest, target)[0] and post(manifest, 'coordkit', target, 'ask',
                       f'anx is down; {summary(record)} -- needs the operator ({why}); relay it or answer if a ruling covers it',
                       record, tier=2, re_id=record['qid']):
        reached.append(target)
    if addressed.to_relay_file(manifest, f'QUESTION anx_down {summary(record)} ({why})'):
        reached.append('relay_file')
    mark(manifest, record['qid'], outage_alerted=at if reached else None, outage_reached=reached)
    return reached


def mark(manifest, qid, **fields):
    def apply(records):
        records[qid].update(fields)
    update(manifest, apply)


def claim(manifest, qid, step, at, guard):
    """Atomically claim one due transition (concurrent sweeps never double-escalate)."""
    def apply(records):
        record = records.get(qid)
        if record is None or not guard(record):
            return False
        record['claim'] = {'step': step, 'at': at}
        return True
    return update(manifest, apply)


YES_NO = re.compile(r"^\W*(approve|may|should|shall|can|could|do|does|is|are|ship|merge|deploy|proceed|go|ok|okay|"
                    r"rerun|retry|skip|keep|use|switch|enable|disable|roll|restart|delete|accept|allow)\b[^?]*\?", re.I | re.S)


def identity_gate(record):
    asker = record.get('asker') or ''
    return ['unknown asker (session not bound to a bus name)'] if not asker or asker.startswith('unknown') else []


def brief_gate(record):
    """Reasons an ask may not reach the operator. Empty when it may. A brief
    needs a known asker, a decision with at least two options (an explicit yes/no question counts as two), and a
    recommendation. Raw captures are evidence for the tier-1 role to brief, never the ask itself."""
    why = []
    asker = record.get('asker') or ''
    if not asker or asker.startswith('unknown-') or asker == 'unknown':
        why.append('unknown asker (session not bound to a bus name)')
    options = [o for o in record.get('options') or [] if str(o).strip()]
    if len(options) < 2 and not YES_NO.match((record.get('question') or '').strip()):
        why.append('no decision with at least two options')
    if not (record.get('recommendation') or '').strip():
        why.append('no recommendation')
    return why


def refuse_brief(manifest, record, why, reasons, at):
    """Not for the operator: hand the raw capture to a live tier-1 agent to brief or answer; with none live, relay only."""
    target = tier1_target(manifest, record['asker'])
    note = '; '.join(reasons)
    relayed = addressed.to_relay_file(manifest, f'QUESTION needs_brief {record["qid"]} ({note}; was going to tier 2: {why}) {summary(record)}'[:600])
    posted = None
    if target:
        try:
            posted = post(manifest, 'coordkit', target, 'ask',
                          f'{record["qid"]} needs a decision brief before it can go to the operator ({note}). Answer it '
                          f'yourself, or re-ask with options and a recommendation. Raw: {summary(record)[:300]}',
                          record, tier=1)
        except (Exception, SystemExit):  # the relay write above still counts; never re-relay
            pass
    if not (posted or relayed):  # a bus row (live only: shadow's post() only relays) or the relay file
        # Delivered nowhere (no live tier 1, no relay file): stay retryable so the obligation is never lost.
        mark(manifest, record['qid'], brief_refused=reasons, tier2_why=why, claim=None, refusal_undelivered_at=at,
             deadline_at=at + duration(addressed.cfg(manifest).realert_after))
        return record['tier']
    mark(manifest, record['qid'], state='escalated', tier=1, target=target, brief_refused_at=at, brief_refused=reasons,
         tier2_why=why, claim=None)
    return 1


def tier2(manifest, record, why, at=None):
    """anx ask with the asker's recommendation, linked both ways; relay file in shadow mode.

    Only a successful anx ask makes tier 2 durable. When anx fails the record stays at its tier with
    `anx_failed_at`, the modal is not denied, and the next sweep retries."""
    at = now() if at is None else at
    from . import briefs
    # Only an authored brief reaches the operator: tier 1's (`question brief`) or one the asker filed with its question. A
    # capture is evidence, never the ask. The captured asker's identity always gates.
    brief = record.get('brief')
    reasons = list(dict.fromkeys(identity_gate(record) + (briefs.validate(brief) if brief else
                                                          brief_gate(record) + ['no decision brief authored yet'])))
    if reasons:
        return refuse_brief(manifest, record, why, reasons, at)
    text = summary(record)
    ask_id = None
    if manifest.mode == 'live':
        from .anxcli import AnxClient, ask_reference
        try:
            result = AnxClient(manifest).ask(manifest.escalation.card, brief)
            ask_id = ask_reference(result)
        except Exception as exc:  # anx down: relay file now, retry on the next sweep
            addressed.to_relay_file(manifest, f'QUESTION anx_ask_failed {record["qid"]}: {exc}'[:300])
        if not ask_id:
            mark(manifest, record['qid'], anx_failed_at=at, tier2_why=why, claim=None)
            if not load_all(manifest)[record['qid']].get('outage_alerted'):
                outage_fallback(manifest, record, why, at)
            return record['tier']
    addressed.to_relay_file(manifest, f'QUESTION tier=2 {text} ({why}; anx={ask_id})')
    post(manifest, 'coordkit', record['asker'], 'fyi',
         f'{record["qid"]} escalated to the operator (anx {ask_id}): {why}. Keep working on anything independent.',
         record, tier=2, re_id=record['qid'])
    mark(manifest, record['qid'], state='escalated', tier=2, anx_ask_id=ask_id, escalated_at=at, tier2_why=why,
         claim=None, anx_failed_at=None, cycle_deadline_at=at + wait_seconds(manifest, record))
    return 2


# -- ladder, answers and closure --------------------------------------------------------------------

RECEIPT = re.compile(r'^\s*(received|ack(nowledged)?|got it|on it|looking)\b[\s.:;,!-]*$', re.I)


def replies(manifest, rows, record):
    """The first real answer to this question from anyone but its asker and coordkit. A receipt
    ("received", an `ack` of received/blocked, or bare acknowledgement text) is not an answer."""
    ids = {record['qid'], record.get('bus_id'), *record.get('bus_ids', [])} - {None}
    def matches(row):
        if not isinstance(row, dict) or not isinstance(row.get('text', ''), str):
            return False
        if row.get('re') not in ids or row.get('frm') in (record['asker'], 'coordkit'):
            return False
        if row.get('kind') not in ('answer', 'decision', 'ruling', 'approval', 'msg'):
            return False
        if row.get('ack') in ('received', 'blocked') or RECEIPT.match(row.get('text') or ''):
            return False
        return True
    answers = [row for row in rows if matches(row)]
    saved = record.get('bus_answer')
    if not matches(saved):
        return next(iter(answers), None)
    if not answers:
        return saved
    first = answers[0]
    # When both are present, the supplied physical log order is authoritative,
    # even for incumbent rows with absent sequences or equal/skewed timestamps.
    if any(row.get('id') == saved.get('id') for row in answers):
        return first
    if type(first.get('seq')) is int and type(saved.get('seq')) is int:
        return first if first['seq'] < saved['seq'] else saved
    try:
        return first if parse_ts(first['ts']) < parse_ts(saved['ts']) else saved
    except (KeyError, TypeError, ValueError, OverflowError):
        return saved


DEFAULT_ROLLBACK = 'post a correcting answer --re to the original ask; re-ask with new information to replace the default'


def wait_seconds(manifest, record):
    return duration(cfg(manifest).urgent_deadline if record.get('urgent') else cfg(manifest).normal_deadline)


def missed_cycle(manifest, record, at):
    """Count a deadline once under the store lock; retries/outages are not cycles."""
    def advance(records):
        current = records[record['qid']]
        deadline = current.get('cycle_deadline_at', current['deadline_at'])
        if current['state'] not in ('open', 'escalated') or at < deadline or (
                current.get('claim') and at - current['claim']['at'] < 300):
            return current
        if current['tier'] == 1 and not current.get('tier1_missed'):
            current['tier1_missed'] = True
        elif current['tier'] != 2 or not current.get('anx_ask_id'):
            return current
        current['cycles'] = current.get('cycles', 0) + 1
        current['cycle_deadline_at'] = at + wait_seconds(manifest, current)
        return current
    return update(manifest, advance)


def apply_default(manifest, record, at, rows=None):
    """Use the shared publisher, then the ladder's normal answer delivery/ANX withdrawal."""
    from . import bus
    if os.environ.get('COORDKIT_QUESTION_GUARD') == 'off':
        return False
    rows = recent_rows(manifest) if rows is None else rows
    answer = replies(manifest, rows, record)
    if answer:
        close(manifest, record, answer.get('frm'), answer.get('text', ''), answer.get('id'))
        return False
    due = lambda r: r['state'] in ('open', 'escalated') and default_due(manifest, r, at) and not (
        r.get('claim') and at - r['claim']['at'] < 300)
    if not claim(manifest, record['qid'], 'default', at, due):
        return False
    def publish(records):
        current = records[record['qid']]
        settle(manifest, current, rows, at)
        return dict(current)
    current = update(manifest, publish)
    if current.get('state') == 'answered' and not current.get('auto_default_applied'):
        close(manifest, current, current['answered_by'], current['answer'], current['answer_id'])
        return False
    if current.get('auto_default_applied'):
        close(manifest, current, 'default', current['default'], current['answer_id'])
        return True
    mark(manifest, record['qid'], claim=None)
    return False


def capture_bus_asks(manifest, rows, at):
    """Backfill the single bus projection; internal ladder hops never become fresh asks."""
    if os.environ.get('COORDKIT_QUESTION_GUARD') == 'off':
        return []
    seen = set(load_all(manifest))
    closed = {r.get('re') for r in rows if isinstance(r, dict)
              and isinstance(r.get('re'), str) and r.get('kind') in ('answer', 'decision', 'ruling', 'approval')}
    out = []
    for row in rows[-10000:]:
        if (not isinstance(row, dict) or row.get('kind') != 'ask' or row.get('question')
                or row.get('copy_of') or row.get('topic') == 'coordkit-digest' or row.get('id') in seen | closed):
            continue
        track_bus_ask(manifest, row)
        seen.add(row['id'])
        out.append(f'QUESTION_CAPTURED {row["id"]} bus {row["id"]}')
    return out


def ladder(manifest, at=None, rows=None, anx_answers=None, anx_known=None):
    """Advance every open question one due step. Returns lines describing what happened."""
    from . import bus
    at = now() if at is None else at
    if os.environ.get('COORDKIT_QUESTION_GUARD') == 'off':
        return []
    bus.configure(manifest)
    rows = recent_rows(manifest) if rows is None else rows
    out = []
    for record in list(load_all(manifest).values()):
        if record.get('capture') == 'bus':
            continue  # reconciled together below; bus asks do not enter the harness/ANX ladder
        if record['state'] == 'answered':
            # Delivery pending: retry until the asker hears it (a re-armed waiter picks up the decision row).
            how = deliver(manifest, record, record.get('answer', ''), record.get('answer_id'))
            if how not in ('relay', 'shadow', 'undelivered'):
                finish(manifest, record, how)
                out.append(f'QUESTION_DELIVERED {record["qid"]} via {how}')
            elif how == 'relay' and (record.get('asker') or '').startswith('unknown') \
                    and at - record.get('answered_at', at) >= UNBOUND_DELIVERY_WINDOW:
                # An unbound asker had an hour to arm a waiter under its name; after that the relay notice is the
                # final delivery, so the record stops counting as open.
                finish(manifest, record, 'relay')
                out.append(f'QUESTION_CLOSED {record["qid"]} relayed (unbound asker)')
            continue
        if record['state'] not in ('open', 'escalated'):
            continue
        if record.get('brief_refused_at') and not record.get('target') and (record.get('asker') or '').startswith('unknown') \
                and at - record['brief_refused_at'] >= UNBOUND_DELIVERY_WINDOW:
            # No bus name and no tier-1 route: the relay notice was its final destination; stop counting it as open.
            mark(manifest, record['qid'], state='closed', closed_at=at, closed_by='relay', claim=None)
            out.append(f'QUESTION_CLOSED {record["qid"]} relayed (unbound asker, refused capture)')
            continue
        answer = replies(manifest, rows, record)
        if answer:
            close(manifest, record, answer.get('frm'), answer.get('text', ''), answer.get('id'))
            out.append(f'QUESTION_ANSWERED {record["qid"]} by {answer.get("frm")}')
            continue
        if record.get('anx_ask_id') and anx_answers:
            got = anx_answers.get(record['anx_ask_id'])
            if got is not None:
                handled = relay_anx_answer(manifest, record, got)
                state = 'QUESTION_ANSWER_PENDING' if handled is False else 'QUESTION_ANSWERED'
                out.append(f'{state} {record["qid"]} by {cfg(manifest).human_actor} (anx)')
                continue
        if record.get('anx_ask_id') and anx_known is not None and record['anx_ask_id'] not in anx_known \
                and at - record.get('escalated_at', at) >= WITHDRAWN_AFTER:
            # Gone from a complete anx listing: withdrawn or retired there. Close it and tell the asker.
            close(manifest, record, 'anx', 'the ask was withdrawn in anx; re-ask with a brief if it still matters')
            out.append(f'QUESTION_CLOSED {record["qid"]} withdrawn in anx')
            continue
        record = missed_cycle(manifest, record, at)
        if apply_default(manifest, record, at, rows):
            out.append(f'QUESTION_DEFAULTED {record["qid"]} default applied after '
                       f'{max(record.get("cycles", 0), record.get("reasks", 0))} cycles')
            continue
        retry = record.get('anx_failed_at') and at - record['anx_failed_at'] >= 300
        if record['tier'] <= 1 and (at >= record['deadline_at'] or retry) and not record.get('brief_refused_at'):
            due = lambda r: r['tier'] <= 1 and r['state'] in ('open', 'escalated') and not (
                r.get('claim') and at - r['claim']['at'] < 300)
            if not claim(manifest, record['qid'], 'tier2', at, due):
                continue
            why = record.get('tier2_why') if retry else (
                f'no answer from {record.get("target")} by the {record["priority"]} deadline'
                if record['tier'] == 1 else 'not routed')
            tier = tier2(manifest, record, why, at)
            out.append(f'QUESTION_ESCALATED {record["qid"]} tier={tier} ({why})')
            continue
    reconcile_defaults(manifest, rows, at)
    return out


def deliver(manifest, record, text, answer_id=None):
    """Make sure the asker hears the answer: its waiter, else a delivery adapter, else the relay."""
    live, why = addressed.listening(manifest, record['asker'])
    if live:
        return 'waiter'
    if manifest.mode != 'live':
        notify_once(manifest, record['qid'], f'QUESTION_ANSWER (shadow) {record["qid"]} for {record["asker"]}: {text[:200]}')
        return 'shadow'
    spec = addressed.registry(manifest).get(record['asker'], {})
    note = f'Answer to your question {record["qid"]}: {text[:300]}'
    if spec.get('adapter') == 'tmux' and spec.get('target'):
        from . import sidecar
        lease = addressed.Lease(manifest, record['asker'])
        lease.data = addressed.read_lease(manifest, record['asker']) or {'unconfirmed': []}
        row = {'id': answer_id or record['qid'], 'frm': 'coordkit', 'kind': 'decision', 'text': note}
        result = sidecar.deliver(manifest, record['asker'], spec['target'], row, lease, out=lambda *_: None)
        if result.startswith('confirmed'):
            return 'sidecar'
    remote = record.get('remote') or spec.get('remote')
    if remote and cfg(manifest).delivery_cli:
        path = home(manifest)/'answers'/f'{record["qid"]}.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(note + '\n')
        try:
            if subprocess.run([cfg(manifest).delivery_cli, 'send', remote, str(path)], capture_output=True,
                              timeout=60).returncode == 0:
                return 'delivery_cli'
        except (OSError, subprocess.SubprocessError):
            pass
    written = notify_once(manifest, record['qid'],
                          f'QUESTION_ANSWER_UNDELIVERED {record["qid"]} to {record["asker"]} ({why}): {text[:200]}')
    return 'relay' if written else 'undelivered'


UNBOUND_DELIVERY_WINDOW = 3600  # seconds an answer to an unbound asker waits for a waiter before closing


def notify_once(manifest, qid, line):
    """Append a pending answer's relay notice exactly once (test-and-set under the store lock, so an
    answer command racing a sweep cannot write it twice). True when it is on the relay file."""
    def apply(records):
        record = records[qid]
        if record.get('relay_notified'):
            return True
        if addressed.to_relay_file(manifest, line):
            record['relay_notified'] = now()
            return True
        return False
    return update(manifest, apply)


def close(manifest, record, by, text, answer_id=None):
    """Record the answer; close once the asker has it. Undelivered answers stay `answered` and the
    sweep retries delivery (the asker's re-armed waiter also picks up the decision row)."""
    def accept(records):
        current = records.get(record['qid'])
        if current is None or current['state'] not in ('open', 'escalated'):
            return None
        current.update(state='answered', answered_by=by, answer=text[:500], answer_id=answer_id,
                       answered_at=now(), claim=None)
        return dict(current)
    record = update(manifest, accept)
    if record is None:
        return 'already'
    if record.get('anx_ask_id') and by not in (cfg(manifest).human_actor, 'anx') and manifest.mode == 'live':  # 'anx': already gone there
        withdraw_anx(manifest, record, by)
    how = deliver(manifest, record, text, answer_id)
    if how in ('relay', 'shadow', 'undelivered'):
        return how
    finish(manifest, record, how)
    return how


def finish(manifest, record, how):
    mark(manifest, record['qid'], state='closed', closed_at=now(), delivered_via=how)


def withdraw_anx(manifest, record, by):
    """An agent answered first: withdraw the anx ask so the operator never answers a settled question."""
    from .anxcli import AnxClient
    ref = record['anx_ask_id']
    ref = ref if str(ref).startswith('event:') else f'event:{ref}'
    try:
        AnxClient(manifest).call('ask', 'withdraw', ref, '--reason', f'answered on the bus by {by}', write=True)
    except Exception as exc:
        addressed.to_relay_file(manifest, f'QUESTION anx_withdraw_failed {record["qid"]} {ref}: {exc}'[:300])


def accept_anx_delivery(manifest, qid, text, answer_id):
    """The same atomic settlement contract applies to every answering actor."""
    record = load_all(manifest).get(qid)
    if record is not None:
        return close(manifest, record, cfg(manifest).human_actor, text, answer_id)


def relay_anx_answer(manifest, record, answer):
    if isinstance(answer, dict) and (answer.get('response_event_id') or answer.get('response_event_ref')):
        from .answers import question_answer
        return question_answer(manifest, record, answer)
    text = str((answer or {}).get('text') or (answer or {}).get('outcome') or answer)
    post(manifest, 'coordkit', record['asker'], 'decision', f'{cfg(manifest).human_actor} answered {record["qid"]}: {text[:300]}',
         record, re_id=record['qid'])
    close(manifest, record, cfg(manifest).human_actor, text)


def anx_answers(manifest):
    """{ask id: answer} from the shared stream, retained before routing to an owner."""
    from .anxcli import AnxClient
    from .process import ToolError
    try:
        result = AnxClient(manifest).call('await', '--answers', '--timeout', '1s', '--settle', '0')
    except ToolError:
        return {}
    out = {}
    for item in (result or {}).get('answers', []):
        for key in ('ask_id', 'event_ref', 'id'):
            if item.get(key):
                out[item[key]] = item.get('answer') or item
                out[f'event:{item[key]}'.replace('event:event:', 'event:')] = item.get('answer') or item
    return out


WITHDRAWN_AFTER = 600  # seconds after escalation before an ask missing from anx counts as withdrawn


def anx_inbox(manifest):
    """(answers {ask id: answer}, known ask ids) from a complete `inbox list --status all` snapshot, or ({}, None)
    when anx is unreachable or the listing is paginated. Unlike `await --answers` this is not a consuming stream, so
    an answer another reader took is still found."""
    from .anxcli import AnxClient
    from .process import ToolError
    try:
        result = AnxClient(manifest).call('inbox', 'list', '--status', 'all') or {}
    except ToolError:
        return {}, None
    if result.get('has_more') or result.get('next_cursor') or result.get('truncated'):
        return {}, None
    answers, known = {}, set()
    for item in result.get('items', []):
        ref = item.get('ask_id')
        if not ref:
            continue
        known.add(ref)
        if item.get('status') == 'answered' or item.get('answer'):
            answers[ref] = item.get('answer') or item
    return answers, known


def answer(manifest, qid, name, text):
    """A tier-1 agent answers: bus decision to the asker, closure, anx withdrawn if filed."""
    manifest.writable()
    record = load_all(manifest).get(qid)
    if record is None:
        raise ManifestError(f'unknown_question: {qid}')
    if record['state'] in ('closed', 'answered'):
        print(f'already {record["state"]} by {record.get("answered_by")}')
        return 0
    row_id = post(manifest, name, record['asker'], 'decision', f'{qid}: {text}', record, re_id=qid)
    how = close(manifest, record, name, text, row_id)
    print(f'QUESTION_ANSWERED {qid} by {name} delivered={how}')
    return 0


# -- transcripts ------------------------------------------------------------------------------------

def last_assistant_text(path, limit=400000):
    """Text of the last assistant message in a Claude Code transcript (bounded tail read)."""
    try:
        with open(path, 'rb') as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - limit))
            lines = stream.read().decode(errors='replace').splitlines()
    except OSError:
        return ''
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        message = row.get('message') or {}
        if row.get('type') == 'assistant' or message.get('role') == 'assistant':
            content = message.get('content')
            if isinstance(content, str):
                return content
            texts = [c.get('text', '') for c in content or [] if isinstance(c, dict) and c.get('type') == 'text']
            if texts:
                return '\n'.join(texts)
    return ''


def prose_question(text, human_actor=""):
    """True when a turn ends by asking the human for a decision."""
    paragraphs = [p.strip() for p in text.strip().split('\n\n') if p.strip()]
    if not paragraphs:
        return False
    tail = paragraphs[-1][-600:]
    human_wait = (re.search(r'\bwaiting\s+(?:on|for)\s+' + re.escape(human_actor) + r'(?![\w+-]|\.\w)',
                            tail, re.I) if human_actor else None)
    if PROSE.search(tail) or human_wait:
        return True
    return tail.rstrip().endswith('?') and re.search(r'\byou(r)?\b', tail, re.I) is not None


def pending_modals(path, min_age, at=None):
    """[(tool_use_id, timestamp, input)] AskUserQuestion uses with no tool_result, older than min_age."""
    at = now() if at is None else at
    uses, results = {}, set()
    try:
        with open(path, errors='replace') as stream:
            for line in stream:
                if 'AskUserQuestion' not in line and 'tool_result' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                content = (row.get('message') or {}).get('content')
                for c in content if isinstance(content, list) else []:
                    if not isinstance(c, dict):
                        continue
                    if c.get('type') == 'tool_use' and c.get('name') == 'AskUserQuestion':
                        uses[c.get('id')] = (row.get('timestamp'), c.get('input') or {}, row.get('sessionId'))
                    elif c.get('type') == 'tool_result':
                        results.add(c.get('tool_use_id'))
    except OSError:
        return []
    out = []
    for use_id, (ts, tool_input, session) in uses.items():
        if use_id in results or not ts:
            continue
        if at - parse_ts(ts).timestamp() >= min_age:
            out.append((use_id, ts, tool_input, session))
    return out


def scan_transcripts(manifest, at=None):
    """Sweep fallback: pending modals in managed config dirs that no hook caught."""
    at = now() if at is None else at
    found = []
    window = cfg(manifest).transcript_window_hours*3600
    for config_dir in cfg(manifest).config_dirs:
        base = Path(config_dir).expanduser()
        for path in glob.glob(str(base/'projects'/'*'/'*.jsonl')):
            try:
                if at - os.path.getmtime(path) > window:
                    continue
            except OSError:
                continue
            for use_id, ts, tool_input, session in pending_modals(path, duration(cfg(manifest).modal_min_age), at):
                question, options, rec = from_tool_input(tool_input)
                asker, how = asker_for(manifest, session or Path(path).stem, path)
                record, fresh = create(manifest, asker, question or '(empty AskUserQuestion)', options=options,
                                       recommendation=rec, session_id=session or Path(path).stem, remote=remote_of(how),
                                       capture='sweep', config_dir=str(base), at=at)
                if record and fresh:
                    found.append(f'QUESTION_CAPTURED {record["qid"]} sweep {asker}: {question[:80]}')
    return found


# -- hooks ------------------------------------------------------------------------------------------

def hook_disabled(manifest):
    return os.environ.get('COORDKIT_QUESTION_GUARD') == 'off' or manifest.mode != 'live'


def hook_pretooluse(manifest, payload, config_dir=''):
    """Deny an AskUserQuestion only after it is posted; otherwise allow it untouched."""
    if hook_disabled(manifest) or payload.get('tool_name') != 'AskUserQuestion':
        return None
    question, options, rec = from_tool_input(payload.get('tool_input') or {})
    if not question:
        return None
    asker, how = asker_for(manifest, payload.get('session_id'), payload.get('transcript_path'))
    record, _ = create(manifest, asker, question, options=options, recommendation=rec, remote=remote_of(how),
                       session_id=payload.get('session_id') or '', capture='pretooluse', config_dir=config_dir)
    if record is None:
        return None  # an excluded (human) session: the modal shows as usual
    record = load_all(manifest)[record['qid']]
    # Deny only when a durable post exists: a tier-1 bus ask, or a tier-2 anx ask with its id.
    where = record.get('target') if record['tier'] == 1 else \
        f'{cfg(manifest).human_actor} via anx' if record['tier'] >= 2 and record.get('anx_ask_id') else None
    if not where:
        return None
    reason = (f'Posted as question {record["qid"]} to {where} (deadline {iso(record["deadline_at"])[11:16]}Z); '
              f'your bus waiter delivers the answer. Keep working on anything independent.')
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                   'permissionDecisionReason': reason}}


def hook_stop(manifest, payload, config_dir=''):
    """A prose decision question at turn end with no armed waiter becomes a question."""
    if hook_disabled(manifest) or payload.get('stop_hook_active'):
        return None
    path = payload.get('transcript_path')
    text = last_assistant_text(path) if path else ''
    if not text or not prose_question(text, cfg(manifest).human_actor):
        return None
    asker, how = asker_for(manifest, payload.get('session_id'), path)
    if addressed.listening(manifest, asker)[0]:
        return None
    tail = [p for p in text.strip().split('\n\n') if p.strip()][-1][-400:]
    if cfg(manifest).prose_detector != 'post':
        addressed.to_relay_file(manifest, f'QUESTION_PROSE (log-only) {asker} ended its turn asking, with no waiter: {tail[:200]}')
        return None
    create(manifest, asker, tail, session_id=payload.get('session_id') or '', capture='stop', config_dir=config_dir,
           remote=remote_of(how))
    return None


def hook_notification(manifest, payload, config_dir=''):
    """Journal 'waiting for input/permission' notices; the sweep turns stale ones into questions."""
    if hook_disabled(manifest):
        return None
    kind = payload.get('notification_type') or ''
    message = str(payload.get('message', ''))
    # Idle-at-prompt is not a question. Every idle lane waiting on its bus waiter
    # (and the operator's own chat) sent 'waiting for your input' and became an empty ANX ask. Only permission prompts count.
    if kind != 'permission_prompt' and not re.search(r'permission', message, re.I):
        return None  # idle_prompt, auth_success, plugin notices and the like are not questions
    session = payload.get('session_id') or 'unknown'
    path = home(manifest)/'notifications'/f'{session}.json'
    size = os.path.getsize(payload['transcript_path']) if payload.get('transcript_path') and \
        os.path.exists(payload['transcript_path']) else None
    write_json(path, {'message': message[:300], 'type': kind, 'at': now(), 'transcript': payload.get('transcript_path'),
                      'transcript_size': size, 'config_dir': config_dir})
    return None


def scan_notifications(manifest, at=None):
    at = now() if at is None else at
    out = []
    for path in sorted((home(manifest)/'notifications').glob('*.json')):
        item = read_json(path, {}) or {}
        transcript = item.get('transcript')
        grew = transcript and os.path.exists(transcript) and os.path.getsize(transcript) != item.get('transcript_size')
        if grew:
            path.unlink(missing_ok=True)
            continue
        if at - item.get('at', at) < duration(cfg(manifest).notification_idle):
            continue
        if item.get('type') != 'permission_prompt' and 'permission' not in item.get('message', '').lower():
            path.unlink(missing_ok=True)  # Idle notices do not request a decision.
            continue
        asker, _ = asker_for(manifest, path.stem, transcript)
        # Not a brief: tier 1 decides whether the operator is needed; the gate keeps it from ANX.
        record, fresh = create(manifest, asker, f'Lane {asker} is blocked on a permission prompt: {item.get("message", "")[:200]}',
                               session_id=path.stem, capture='permission', urgent=True,
                               config_dir=item.get('config_dir', ''), at=at)
        path.unlink(missing_ok=True)
        if record and fresh:
            out.append(f'QUESTION_CAPTURED {record["qid"]} notification {asker}')
    return out


HOOK_DEADLINE = 4  # seconds; the installed command adds a 5s perl alarm as the backstop


def run_hook(manifest, event, config_dir, stdin=None):
    """Hook entry point: never raises, never blocks; prints a decision only for a posted question."""
    import signal
    def overrun(signum, frame):
        raise TimeoutError('question hook deadline')
    armed = hasattr(signal, 'SIGALRM') and stdin is None  # real hook process (tests pass stdin)
    try:
        if armed:
            # Inside the 5s perl backstop: a TimeoutError interrupts any tool call, and process.run kills
            # the child's process group on the way out, so no anx ask outlives the hook.
            signal.signal(signal.SIGALRM, overrun)
            signal.alarm(HOOK_DEADLINE)
        payload = json.loads((stdin if stdin is not None else sys.stdin.read()) or '{}')
        fn = {'pretooluse': hook_pretooluse, 'stop': hook_stop, 'notification': hook_notification}[event]
        decision = fn(manifest, payload, config_dir)
        if armed:
            signal.alarm(0)
        if decision:
            print(json.dumps(decision))
    except Exception:  # fail open: a broken hook must never block a tool call
        pass
    return 0


# -- status and CLI ---------------------------------------------------------------------------------

def open_questions(manifest, at=None):
    at = now() if at is None else at
    # `answered` with delivery pending is still open: the asker has not heard it yet.
    return [dict(r, age=at - r['created_at']) for r in load_all(manifest).values()
            if r['state'] in ('open', 'escalated', 'answered')]


def status_line(manifest, at=None):
    items = open_questions(manifest, at)
    over = [i for i in items if i['age'] >= 1800]
    oldest = int(max((i['age'] for i in items), default=0) // 60)
    defaulted = sum(bool(r.get('auto_default_applied')) for r in load_all(manifest).values())
    return f'QUESTIONS open={len(items)} over30m={len(over)} oldest={oldest}m' + (
        f' defaulted={defaulted}' if defaulted else '')


def sweep(manifest, at=None, out=print):
    from . import bus
    if os.environ.get('COORDKIT_QUESTION_GUARD') == 'off':
        out(status_line(manifest, at))
        return []
    bus.configure(manifest)
    rows = recent_rows(manifest)
    lines = capture_bus_asks(manifest, rows, now() if at is None else at)
    lines += scan_transcripts(manifest, at) + scan_notifications(manifest, at)
    answers, known = {}, None
    if manifest.mode == 'live' and any(r.get('anx_ask_id') for r in open_questions(manifest, at)):
        answers = anx_answers(manifest)
        listed, known = anx_inbox(manifest)
        answers = dict(listed, **answers)
    lines += ladder(manifest, at, rows=rows, anx_answers=answers, anx_known=known)
    for line in lines:
        out(line)
    for record in open_questions(manifest, at):
        if record.get('default_deferred') and not record.get('default_deferred_swept'):
            line = f"QUESTION_DEFERRED {record['qid']}: {record['default_deferred']}"
            out(line)
            mark(manifest, record['qid'], default_deferred_swept=now() if at is None else at)
            lines.append(line)
    out(status_line(manifest, at))
    return lines


def author_brief(manifest, qid, name, brief, preview=False):
    """Tier 1 turns a capture into a decision brief; a valid one goes to the operator at once (tier 2)."""
    from . import briefs
    record = load_all(manifest).get(qid)
    if record is None:
        raise ManifestError(f'unknown_question: {qid}')
    brief = briefs.clean(dict(brief, asker=record['asker'], actor=brief.get('actor') or record['asker'],  # asker is immutable
                              details=(brief.get('details') or '') + f"\nquestion {qid}; raw: {record['question'][:1000]}"))
    errors = briefs.validate(brief)
    if errors:
        print('BRIEF_INVALID ' + '; '.join(errors))
        return 2
    if preview:
        print('BRIEF_PREVIEW ' + json.dumps(briefs.anx_args(brief)))
        return 0
    manifest.writable()
    if record['state'] not in ('open', 'escalated') or record['tier'] >= 2:
        print(f'BRIEF_NOT_SENT {qid} is {record["state"]} at tier {record["tier"]}')
        return 2
    mark(manifest, qid, brief=brief, brief_by=name, brief_refused_at=None)
    tier = tier2(manifest, load_all(manifest)[qid], f'briefed by {name}')
    print(f'BRIEF_SENT {qid} tier={tier}')
    return 0 if tier == 2 else 1


def main(argv, manifest):
    p = argparse.ArgumentParser(prog='coordkit question', allow_abbrev=False)
    sub = p.add_subparsers(dest='cmd', required=True)
    h = sub.add_parser('hook')
    h.add_argument('event', choices=['pretooluse', 'stop', 'notification'])
    h.add_argument('--config-dir', default='')
    ls = sub.add_parser('list')
    ls.add_argument('--all', action='store_true')
    ls.add_argument('--json', action='store_true')
    a = sub.add_parser('answer')
    a.add_argument('qid')
    a.add_argument('--as', dest='name', required=True)
    a.add_argument('text')
    b = sub.add_parser('bind', help='bind a harness session id to a bus name')
    b.add_argument('--session', required=True)
    b.add_argument('--as', dest='name', required=True)
    b.add_argument('--remote', default='')
    br = sub.add_parser('brief', help='tier 1: attach a decision brief (JSON) to a question and send it to the operator')
    br.add_argument('qid')
    br.add_argument('--as', dest='name', required=True)
    br.add_argument('--from-file', required=True, help='JSON: title, situation, impact, options[{action,consequence,'
                    'reversible}], recommendation, why, if_no_answer, by, evidence[{label,url}], details')
    br.add_argument('--preview', action='store_true', help='validate and print the anx ask; file nothing')
    sub.add_parser('sweep')
    hs = sub.add_parser('heuristic', help='measure the prose detector on recent transcripts (read-only)')
    hs.add_argument('--sample', type=int, default=200)
    o = p.parse_args(argv)
    if o.cmd == 'hook':
        return run_hook(manifest, o.event, o.config_dir)
    if o.cmd == 'list':
        rows = list(load_all(manifest).values()) if o.all else open_questions(manifest)
        if o.json:
            print(json.dumps(rows))
        else:
            for r in sorted(rows, key=lambda r: r['created_at']):
                age = int((now() - r['created_at']) // 60)
                print(f'{r["qid"]} {r["state"]} tier={r["tier"]} age={age}m asker={r["asker"]} '
                      f'target={r.get("target")} anx={r.get("anx_ask_id")} {summary(r)}')
            print(status_line(manifest))
        return 0
    if o.cmd == 'answer':
        return answer(manifest, o.qid, o.name, o.text)
    if o.cmd == 'bind':
        def add(data):
            data[o.session] = {'name': o.name, 'remote': o.remote, 'at': now()}
        path = home(manifest)/'sessions.json'
        with addressed.locked(home(manifest)/'sessions.lock'):
            data = read_json(path, {}) or {}
            add(data)
            write_json(path, data)
        print(f'BOUND {o.session} -> {o.name}')
        return 0
    if o.cmd == 'brief':
        return author_brief(manifest, o.qid, o.name, json.loads(Path(o.from_file).read_text()), o.preview)
    if o.cmd == 'sweep':
        manifest.writable()
        sweep(manifest)
        return 0
    if o.cmd == 'heuristic':
        return heuristic(manifest, o.sample)
    raise ManifestError(f'not_implemented: question {o.cmd}')


def heuristic(manifest, sample):
    """Read-only: how often the prose detector fires on the final messages of recent transcripts."""
    dirs = cfg(manifest).config_dirs or ['~/.claude']
    paths = sorted((p for d in dirs for p in glob.glob(str(Path(d).expanduser()/'projects'/'*'/'*.jsonl'))),
                   key=lambda p: os.path.getmtime(p), reverse=True)[:sample]
    hits = 0
    for path in paths:
        text = last_assistant_text(path)
        if text and prose_question(text, cfg(manifest).human_actor):
            hits += 1
            tail = [p for p in text.strip().split('\n\n') if p.strip()][-1]
            print(f'HIT {Path(path).stem[:8]} {" ".join(tail.split())[-160:]}')
    print(f'HEURISTIC sample={len(paths)} hits={hits} rate={hits/max(1, len(paths)):.1%}')
    return 0
