"""Correlate direct loop asks across recovery and either shared answer consumer."""
import contextlib
import hashlib
import re
import sys

from . import bus
from .process import ToolError
from .state import read_json, write_json


def reference(ask_id):
    return ask_id if ask_id.startswith('event:') else 'event:' + ask_id


def path(manifest, ask_id):
    key = hashlib.sha256(reference(ask_id).encode()).hexdigest()
    return manifest.paths.root/'state/loop-asks'/(key + '.json')


def defer(manifest, item):
    """Every consuming reader must hand replies to reconciliation durably.

    Retain all consumed replies, including ones whose producer has not yet saved
    its correlation ID. The normal owner can deliver now; reconciliation dedupes.
    """
    ask_id = item.get('ask_id') or item.get('event_ref') or item.get('id')
    if not isinstance(ask_id, str) or not ask_id:
        return
    manifest.writable()
    key = hashlib.sha256(reference(ask_id).encode()).hexdigest()
    write_json(manifest.paths.root/'reconcile/received-answers'/(key + '.json'), dict(item, ask_id=ask_id))


def deferred(manifest):
    return [(p, read_json(p)) for p in (manifest.paths.root/'reconcile/received-answers').glob('*.json')]


def remember(manifest, ask_id, card, name, cycle):
    manifest.writable()
    target = path(manifest, ask_id)
    if not target.exists():
        write_json(target, {'ask_id': reference(ask_id), 'card': card, 'loop': name, 'cycle': cycle})


def receive(manifest, item):
    """Retain a recognized answer before relay; return False for another producer's ask."""
    ask_id = item.get('ask_id') or item.get('event_ref') or item.get('id')
    if not isinstance(ask_id, str) or not ask_id:
        return False
    from .answers import queued
    if queued(manifest, item):
        return True  # full reply already retained by the requester-scoped consumer
    target = path(manifest, ask_id)
    record = read_json(target)
    if not record:
        return False
    manifest.writable()
    record['answer'] = item.get('answer') or item
    pending = target.with_suffix('.answer.json')
    write_json(pending, record)
    write_json(target, record)  # retain earlier rulings after successful bus delivery
    deliver(manifest, pending)
    return True


def answer_text(answer):
    return str(answer.get('text') or answer.get('outcome') or 'ANX answer received') if isinstance(answer, dict) else str(answer)


def retained_answers(report):
    """Migrate context by its own provenance; the predecessor may have advanced."""
    answers = dict(report.get('previous_answers') or {})
    text = report.get('previous_answer')
    if text and text not in answers.values():
        ref, separator, _ = text.partition(': ')
        legacy_key = 'legacy:' + hashlib.sha256(text.encode()).hexdigest()
        key = ref if separator and re.fullmatch(r'event:[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', ref) else legacy_key
        answers[key if key not in answers else legacy_key] = text
    return answers


def answer_context(manifest, ask_id):
    """Read earlier answers without consuming the shared reply stream.

    Bound pagination and refuse an incomplete lookup rather than replacing an
    answered question without its context. Local receipts survive supersession.
    """
    from .anxcli import AnxClient
    record = read_json(path(manifest, ask_id)) or {}
    if record.get('answer'):
        return reference(ask_id) + ': ' + answer_text(record['answer'])
    cursor, seen = None, set()
    for _ in range(10):
        result = AnxClient(manifest).call('inbox', 'list', '--status', 'all', '--limit', '200',
                                         *(['--cursor', cursor] if cursor else [])) or {}
        result = result.get('body', result)
        for item in result.get('items', []):
            if item.get('ask_id') and reference(item['ask_id']) == reference(ask_id):
                if item.get('answer'):
                    receive(manifest, item)
                    return reference(ask_id) + ': ' + answer_text(item['answer'])
                return ''
        cursor = result.get('next_cursor')
        if not cursor and not (result.get('has_more') or result.get('truncated')):
            return ''
        if not cursor or cursor in seen:
            break
        seen.add(cursor)
    raise ToolError('anx', 6, 'earlier loop answer lookup incomplete; reminder retained for retry', 'protocol_error')


def deliver(manifest, pending):
    record = read_json(pending)
    if not record:
        return
    from .answers import already_delivered, queued
    if already_delivered(manifest, record):
        pending.unlink(missing_ok=True)
        return
    if queued(manifest, record):
        return  # requester-scoped receipt owns the retry; do not race its publication
    answer = record['answer']
    text = answer_text(answer)
    owner = manifest.escalation.internal_to or manifest.obligations.coordinator or 'lead'
    try:
        with contextlib.redirect_stdout(sys.stderr):
            bus.main(['post', '--from', 'coordkit', '--to', owner, '--kind', 'decision',
                      '--topic', 'loop-failure-answer', '--subject', record['card'],
                      '--rollback', 'Follow up on the original ANX ask to revise this answer.',
                      '--id', 'answer-' + record['ask_id'], '--force', '--no-mirror', text], manifest)
        pending.unlink(missing_ok=True)
    except (SystemExit, ToolError, OSError, ValueError) as exc:
        print(f'LOOP_ANSWER_PENDING {record["ask_id"]}: {exc}', file=sys.stderr)


def drain(manifest):
    for pending in (manifest.paths.root/'state/loop-asks').glob('*.answer.json'):
        deliver(manifest, pending)
