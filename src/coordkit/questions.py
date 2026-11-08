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
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

from . import addressed
from .manifest import ManifestError, duration
from .process import parse_ts
from .state import read_json, write_json

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
    return read_json(home(manifest)/'questions.json', {}) or {}


CLOSED_RETENTION = 7*86400


def save_all(manifest, records):
    # Closed questions are kept a week for audit, then pruned; open ones stay until answered.
    cutoff = now() - CLOSED_RETENTION
    for qid in [q for q, r in records.items() if r.get('state') == 'closed' and r.get('closed_at', 0) < cutoff]:
        del records[qid]
    write_json(home(manifest)/'questions.json', records)


def update(manifest, fn):
    """Run fn(records) under the store lock and persist; returns fn's result."""
    with addressed.locked(home(manifest)/'questions.lock'):
        records = load_all(manifest)
        result = fn(records)
        save_all(manifest, records)
        return result


def normalize(text):
    """Remove transient clocks/ids/counts, preserving PR numbers and flag identities."""
    tokens = re.findall(r'#\d+|\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b|\b[qQ]-[0-9a-fA-F]{6}\b|'
                        r'\b\d{1,2}:\d{2}(?:[Zz])?\b|\b\d+\b|\s+|.', text)
    kept = [token for token in tokens if not re.fullmatch(
        r'[qQ]-[0-9a-fA-F]{6}|\d{1,2}:\d{2}(?:[Zz])?|\d+', token)]
    return ' '.join(''.join(kept).lower().split())


def fingerprint(session, text, options=(), recommendation='', default=''):
    """Same session, same question, same choices: a modal repeated with other options is a new question."""
    body = json.dumps([session, normalize(text), list(options), recommendation, default])
    return hashlib.sha256(body.encode()).hexdigest()[:20]


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
           urgent=False, config_dir='', remote='', at=None, brief=None, default='', bus_id=None, bus_target=None):
    """A new open record (or the existing open one for the same session+question). Routes it.
    (None, False) for a session that must never be captured."""
    at = now() if at is None else at
    if session_id in cfg(manifest).exclude_sessions or asker in cfg(manifest).human_sessions:
        return None, False
    default = default.strip()
    fp = fingerprint(session_id or asker, question, tuple(options), recommendation, default)
    is_guarded, why = guarded(manifest, ' '.join([question, *options]))

    def add(records):
        for record in records.values():
            if record['state'] not in ('open', 'escalated', 'answered'):
                continue
            existing = fingerprint(record.get('session_id') or record['asker'], record['question'],
                                   record.get('options', ()), record.get('recommendation', ''), record.get('default', ''))
            if record.get('fingerprint') == fp or existing == fp:
                record['fingerprint'] = fp
                record['last_seen'] = at
                record['reopens'] = record.get('reopens', 0) + 1
                if bus_id:
                    record.setdefault('bus_ids', [record.get('bus_id')])
                    record['bus_ids'].append(bus_id)
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
        if brief:  # filed by the asker with its question; the asker stays the captured one
            from . import briefs
            record['brief'] = briefs.clean(dict(brief, asker=asker, actor=brief.get('actor') or asker,
                                                details=(brief.get('details') or '') + f'\nquestion {qid}'))
        records[qid] = record
        return record, True
    record, fresh = update(manifest, add)
    if fresh:
        route(manifest, record, at)
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
    reasked = f' re-asked {record["reopens"]}x' if record.get('reopens') else ''
    return f'Q {record["qid"]} from {record["asker"]}: {record["question"][:220]}{rec}{opts}{default}{reasked}'


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


def post(manifest, frm, to, kind, text, record, bus_id=None, tier=None, re_id=None, rollback=None):
    """A bus row (live mode). In shadow mode the relay file carries it instead."""
    from . import bus
    if manifest.mode != 'live':
        addressed.to_relay_file(manifest, f'QUESTION tier={tier} {text}')
        return None
    bus.configure(manifest)
    if rollback:
        # Use the normal publication path so a long default has a durable full-text ref.
        bus.main(['post', '--from', frm, '--to', to, '--kind', kind, '--topic', 'question',
                  '--id', bus_id, '--re', re_id, '--rollback', rollback, '--priority',
                  record.get('priority', 'normal'), '--no-mirror', '--force', text], manifest)
        return bus_id
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
        from .anxcli import AnxClient
        try:
            result = AnxClient(manifest).ask(manifest.escalation.card, brief)
            ask_id = (result or {}).get('ask_id') or (result or {}).get('id') or ((result or {}).get('ask') or {}).get('id') \
                or (result or {}).get('event_ref')
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
    for row in rows:
        if row.get('re') not in ids or row.get('frm') in (record['asker'], 'coordkit'):
            continue
        if row.get('kind') not in ('answer', 'decision', 'ruling', 'msg'):
            continue
        if row.get('ack') in ('received', 'blocked') or RECEIPT.match(row.get('text') or ''):
            continue
        return row
    return None


DEFAULT_ROLLBACK = 're-open by posting an answer --re; default was applied after two unanswered cycles'


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


def apply_default(manifest, record, at):
    """Publish an auditable decision before marking the default answered and delivering it."""
    from . import bus
    due = lambda r: r['state'] in ('open', 'escalated') and r.get('cycles', 0) >= 2 and r.get('default') and not (
        r.get('claim') and at - r['claim']['at'] < 300)
    if not claim(manifest, record['qid'], 'default', at, due):
        return False
    answer_id = 'qd-' + record['qid']
    if not any(row.get('id') == answer_id for row in bus.read()):
        post(manifest, 'coordkit', record['asker'], 'decision', record['default'], record,
             bus_id=answer_id, re_id=record.get('bus_id') or record['qid'], rollback=DEFAULT_ROLLBACK)
    mark(manifest, record['qid'], applied_default=True)
    close(manifest, record, 'default', record['default'], answer_id)
    return True


def capture_bus_asks(manifest, rows, at):
    """An explicit --default opts a bus ask into the durable question ladder."""
    records = load_all(manifest)
    seen = {rid for r in records.values() for rid in [r['qid'], r.get('bus_id'), *r.get('bus_ids', [])]}
    closed = {r.get('re') for r in rows if r.get('kind') in ('answer', 'decision', 'reply')}
    out = []
    for row in rows:
        if row.get('kind') != 'ask' or not row.get('default') or row.get('copy_of') or row['id'] in seen | closed:
            continue
        record, fresh = create(manifest, row['frm'], row.get('text', ''), default=row['default'],
                               capture='bus', bus_id=row['id'], bus_target=row['to'],
                               urgent=row.get('priority') == 'urgent',
                               at=parse_ts(row['ts']).timestamp())
        seen.add(row['id'])
        if record and fresh:
            out.append(f'QUESTION_CAPTURED {record["qid"]} bus {row["id"]}')
    return out


def ladder(manifest, at=None, rows=None, anx_answers=None, anx_known=None):
    """Advance every open question one due step. Returns lines describing what happened."""
    from . import bus
    at = now() if at is None else at
    bus.configure(manifest)
    rows = bus.read() if rows is None else rows
    out = []
    for record in list(load_all(manifest).values()):
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
                relay_anx_answer(manifest, record, got)
                out.append(f'QUESTION_ANSWERED {record["qid"]} by {cfg(manifest).human_actor} (anx)')
                continue
        if record.get('anx_ask_id') and anx_known is not None and record['anx_ask_id'] not in anx_known \
                and at - record.get('escalated_at', at) >= WITHDRAWN_AFTER:
            # Gone from a complete anx listing: withdrawn or retired there. Close it and tell the asker.
            close(manifest, record, 'anx', 'the ask was withdrawn in anx; re-ask with a brief if it still matters')
            out.append(f'QUESTION_CLOSED {record["qid"]} withdrawn in anx')
            continue
        record = missed_cycle(manifest, record, at)
        if record.get('cycles', 0) >= 2 and record.get('default'):
            if apply_default(manifest, record, at):
                out.append(f'QUESTION_DEFAULTED {record["qid"]} default applied after {record["cycles"]} cycles')
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
    if record['state'] == 'closed':
        return 'already'
    if record.get('anx_ask_id') and by not in (cfg(manifest).human_actor, 'anx') and manifest.mode == 'live':  # 'anx': already gone there
        withdraw_anx(manifest, record, by)
    mark(manifest, record['qid'], state='answered', answered_by=by, answer=text[:500], answer_id=answer_id,
         answered_at=now(), claim=None)
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


def relay_anx_answer(manifest, record, answer):
    text = str((answer or {}).get('text') or (answer or {}).get('outcome') or answer)
    post(manifest, 'coordkit', record['asker'], 'decision', f'{cfg(manifest).human_actor} answered {record["qid"]}: {text[:300]}',
         record, re_id=record['qid'])
    close(manifest, record, cfg(manifest).human_actor, text)


def anx_answers(manifest):
    """{ask id: answer} newly available from anx (read-only await)."""
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
    defaulted = sum(bool(r.get('applied_default')) for r in load_all(manifest).values())
    return f'QUESTIONS open={len(items)} over30m={len(over)} oldest={oldest}m' + (
        f' defaulted={defaulted}' if defaulted else '')


def sweep(manifest, at=None, out=print):
    from . import bus
    bus.configure(manifest)
    lines = capture_bus_asks(manifest, bus.read(), now() if at is None else at)
    lines += scan_transcripts(manifest, at) + scan_notifications(manifest, at)
    answers, known = {}, None
    if manifest.mode == 'live' and any(r.get('anx_ask_id') for r in open_questions(manifest, at)):
        answers = anx_answers(manifest)
        listed, known = anx_inbox(manifest)
        answers = dict(listed, **answers)
    lines += ladder(manifest, at, anx_answers=answers, anx_known=known)
    for line in lines:
        out(line)
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
