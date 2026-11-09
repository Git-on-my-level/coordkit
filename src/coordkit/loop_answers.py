"""Correlate direct loop asks across recovery and either shared answer consumer."""
import hashlib
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
    """The question reader must hand other producers' replies to reconciliation durably.

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
    target = path(manifest, ask_id)
    record = read_json(target)
    if not record:
        return False
    manifest.writable()
    pending = target.with_suffix('.answer.json')
    write_json(pending, dict(record, answer=item.get('answer') or item))
    deliver(manifest, pending)
    return True


def deliver(manifest, pending):
    record = read_json(pending)
    if not record:
        return
    answer = record['answer']
    text = str(answer.get('text') or answer.get('outcome') or 'ANX answer received') if isinstance(answer, dict) else str(answer)
    owner = manifest.escalation.internal_to or manifest.obligations.coordinator or 'lead'
    try:
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
