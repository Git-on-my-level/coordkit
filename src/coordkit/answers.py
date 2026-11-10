"""Requester-scoped answer delivery: durable bus append precedes read acknowledgement."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

from . import bus
from .anxcli import AnxClient
from .process import ToolError
from .state import read_json, write_json

MAX_PAGES = 10
MAX_ITEMS = 2000


def home(manifest):
    scope = json.dumps([manifest.anx.workspace, manifest.anx.lead_actor, manifest.anx.base_url])
    return manifest.paths.root/'reconcile/unread-answers'/hashlib.sha256(scope.encode()).hexdigest()[:24]


def reference(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:event:)?[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value):
        raise ValueError('invalid answer ask reference')
    return value if value.startswith('event:') else 'event:' + value


@contextlib.contextmanager
def delivery_lock(manifest):
    directory = home(manifest)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory/'consume.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def owns(client, item):
    """The listing is requester-scoped; reject explicit foreign provenance too."""
    requester = item.get('requester_actor_id')
    if requester is None:
        return True  # older CLI listings omit the requester
    return str(requester).removeprefix('actor:') in {str(identity).removeprefix('actor:') for identity in client.identities()}


def reject(manifest, item):
    """Quarantine invalid input independently so later replies remain deliverable."""
    key = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
    write_json(home(manifest)/('invalid-' + key + '.json'),
               {'item': item, 'phase': 'invalid', 'error': 'invalid answer item'})


def unread(client):
    """Snapshot before acknowledging, so deleting unread entries cannot skip a page."""
    items, cursor, seen = [], None, set()
    for _ in range(MAX_PAGES):
        result = client.call('inbox', 'list', '--status', 'answered', '--unread', '--limit', '200',
                             *(['--cursor', cursor] if cursor else []), timeout=30)
        result = result.get('body', result)
        page = result.get('items')
        if not isinstance(page, list) or len(items) + len(page) > MAX_ITEMS:
            raise ValueError('invalid or oversized unread answer snapshot')
        items.extend(page)
        cursor = result.get('next_cursor')
        if not cursor and not (result.get('has_more') or result.get('truncated')):
            return items
        if not cursor or cursor in seen:
            break
        seen.add(cursor)
    raise ValueError('incomplete unread answer snapshot')


def card_subject(item):
    subject = item.get('subject_ref')
    if not subject and isinstance(item.get('subject'), dict):
        subject = item['subject'].get('ref')
    if not isinstance(subject, str) or not re.fullmatch(r'card:[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', subject):
        raise ValueError('answer has no valid card subject')
    return subject


def route(manifest, item, publication=None):
    from .questions import load_all
    ask_id = reference(item['ask_id'])
    # Exact question correlation retains its original asker and reply edge, even
    # if an agent already closed it before the human answered.
    for record in load_all(manifest).values():
        if record.get('anx_ask_id') and reference(record['anx_ask_id']) == ask_id:
            try:
                subject = card_subject(item)
            except ValueError:
                subject = card_subject({'subject_ref': manifest.escalation.card})
            return subject, record['asker'], record['qid'], record
    subject = card_subject(item)
    rows = bus.read()
    from .briefs import blocker_title
    def matches(row):
        return row.get('subject') == subject and item.get('title') in (
            blocker_title(row), ('Coordkit: ' + row['text'])[:99].rstrip('.?') + '?')
    if publication and publication.get('re'):
        # The immutable reply edge proves the original matching blocker even if
        # another identically titled blocker opened after publication. A title
        # mismatch must still use normal routing, preserving other recipients.
        proven = [r for r in rows if r.get('kind') == 'blocker'
                  and r.get('id') == publication['re'] and matches(r)]
        if len(proven) == 1:
            return subject, proven[0]['frm'], proven[0]['id'], None
    blockers = [r for r in bus.open_blockers(rows) if matches(r)]
    if len(blockers) == 1:
        return subject, blockers[0]['frm'], blockers[0]['id'], None
    # A card-level answer is still work for the configured coordinator. Never
    # infer permission to execute its text or choose one of ambiguous blockers.
    owner = manifest.escalation.internal_to or manifest.obligations.coordinator
    if not owner:
        raise ValueError('answer has no configured coordinating lane')
    return subject, owner, None, None


def answer_id(item):
    answer = item['answer']
    value = answer.get('response_event_id') or answer.get('response_event_ref')
    return reference(value).removeprefix('event:')


def publication_id(item):
    return 'answer-' + reference(item['ask_id']) + '-' + hashlib.sha256(answer_id(item).encode()).hexdigest()[:16]


def receipt_path(manifest, item):
    # Distinct responses to one ask keep distinct receipts, including a newer
    # reply arriving while delivery of the previous response is still pending.
    answer = item.get('answer') or {}
    response = answer.get('response_event_id') or answer.get('response_event_ref')
    if isinstance(response, str):
        response = response.removeprefix('event:')
    # Without an event ID, keep conflicting routing metadata as separate work.
    # Otherwise a local delivery on one card could overwrite another's receipt.
    identity = response or {'answer': answer, 'subject': item.get('subject_ref') or item.get('subject'),
                            'title': item.get('title')}
    key = json.dumps([reference(item['ask_id']), identity], sort_keys=True)
    return home(manifest)/(hashlib.sha256(key.encode()).hexdigest() + '.json')


def already_delivered(manifest, item):
    if not isinstance(item, dict) or not isinstance(item.get('ask_id'), str) or not isinstance(item.get('answer'), dict):
        return False
    try:
        record = read_json(receipt_path(manifest, item), {})
    except ValueError:
        return False
    return record.get('phase') in ('delivered', 'read', 'legacy-delivered')


def delivered(row, subject, owner, text):
    if row.get('kind') != 'decision' or row.get('subject') != subject or bus.resolve(row.get('to')) != bus.resolve(owner):
        return False
    if row.get('text') == text:
        return True
    spill = row.get('spill_ref')
    if not spill or row.get('ref') != spill:
        return False
    path = Path(spill).resolve()
    if not path.is_relative_to((bus.STATE/'refs').resolve()):
        return False
    with path.open('rb') as stream:
        data = stream.read(1024 * 1024 + 2)
        if data != (text + '\n').encode():
            return False
        os.fsync(stream.fileno())
    return True


def legacy_route(manifest, item, publication=None):
    from .loop_answers import path as loop_ask_path
    loop_record = read_json(loop_ask_path(manifest, reference(item['ask_id'])), {})
    if loop_record and not (item.get('subject_ref') or item.get('subject')):
        item = dict(item, subject_ref=loop_record['card'])
    return route(manifest, item, publication)


def legacy_path(manifest, ask_id):
    return home(manifest)/('legacy-' + hashlib.sha256(reference(ask_id).encode()).hexdigest() + '.json')


def legacy_match(manifest, item, owner=None):
    """Find a verified publication for a response-less loop representation."""
    ask_id = reference(item['ask_id'])
    text = str(item['answer'].get('text') or item['answer'].get('outcome') or '')
    prefix = 'answer-' + ask_id
    rows = bus.read()
    for row in reversed(rows):
        row_id = row.get('id', '')
        if not isinstance(row_id, str):
            continue
        if (row_id == prefix or re.fullmatch(re.escape(prefix) + r'-[0-9a-f]{16}', row_id)):
            try:
                if owner is None:
                    subject, target, _, _ = legacy_route(manifest, item, row)
                else:
                    subject, target = card_subject(item), owner
            except ValueError:
                continue
            if delivered(row, subject, target, text):
                return row_id
    return None


def record_legacy_delivery(manifest, item, owner, row_id, response_id=None, pending=None):
    """Retain local delivery without claiming an unavailable source read ack.

    The ask-only publication can be bound to at most one subsequently observed
    response event. Further event IDs always keep separate publications.
    Caller holds consume.lock and has verified the full bus payload.
    """
    ask_id = reference(item['ask_id'])
    text = str(item['answer'].get('text') or item['answer'].get('outcome') or '')
    local = {'ask_id': ask_id, 'subject_ref': card_subject(item), 'answer': {'text': text}}
    record = {'item': local, 'owner': owner, 'phase': 'legacy-delivered',
              'bus_id': row_id, 'source_read': False}
    if row_id == 'answer-' + ask_id:
        previous = read_json(legacy_path(manifest, ask_id), {})
        binding = previous.get('response_event_id') or response_id
        if binding:
            record['response_event_id'] = binding
        write_json(legacy_path(manifest, ask_id), record)
    write_json(receipt_path(manifest, local), record)
    settle_legacy_receipts(manifest, item, row_id, pending)


def legacy_pending(manifest):
    """Index response-less pending receipts once per sweep, rather than per reply."""
    pending = {}
    for path in home(manifest).glob('*.json'):
        record = read_json(path, {})
        item = record.get('item')
        if record.get('phase') != 'pending' or not isinstance(item, dict):
            continue
        answer = item.get('answer')
        if not isinstance(answer, dict) or answer.get('response_event_id') or answer.get('response_event_ref'):
            continue
        key = (item.get('ask_id'), str(answer.get('text') or answer.get('outcome') or ''))
        pending.setdefault(key, []).append(path)
    return pending


def settle_legacy_receipts(manifest, item, row_id, pending=None):
    ask_id = reference(item['ask_id'])
    text = str(item['answer'].get('text') or item['answer'].get('outcome') or '')
    paths = home(manifest).glob('*.json') if pending is None else pending.pop((ask_id, text), [])
    row = next((r for r in bus.read() if r.get('id') == row_id), None)
    if row is None:
        return
    for path in paths:
        queued = read_json(path, {})
        if queued.get('phase') != 'pending':
            continue
        payload = queued.get('item', {})
        if not isinstance(payload, dict):
            continue
        answer = payload.get('answer', {})
        if (payload.get('ask_id') == ask_id
                and isinstance(answer, dict) and not (answer.get('response_event_id') or answer.get('response_event_ref'))
                and str(answer.get('text') or answer.get('outcome') or '') == text):
            try:
                subject, owner, _, _ = legacy_route(manifest, payload, row)
            except ValueError:
                if pending is not None:
                    pending.setdefault((ask_id, text), []).append(path)
                continue
            if delivered(row, subject, owner, text):
                queued.update(phase='legacy-delivered', bus_id=row_id, source_read=False)
                write_json(path, queued)
            elif pending is not None:
                pending.setdefault((ask_id, text), []).append(path)


def stage(manifest, item):
    item = dict(item, ask_id=reference(item['ask_id']))
    if isinstance(item.get('answer'), str):
        item['answer'] = {'text': item['answer']}
    path = receipt_path(manifest, item)
    existing = read_json(path, {})
    answer = item['answer']
    if not (answer.get('response_event_id') or answer.get('response_event_ref')):
        bus.configure(manifest)
        row_id = legacy_match(manifest, item)
        if row_id:
            row = next(r for r in bus.read() if r.get('id') == row_id)
            subject, owner, _, _ = legacy_route(manifest, item, row)
            record_legacy_delivery(manifest, dict(item, subject_ref=subject), owner, row_id)
            write_json(path, {'item': item, 'phase': 'legacy-delivered',
                              'bus_id': row_id, 'source_read': False})
            return path
    # This path identifies one immutable response event. Different CLI shapes
    # of that event must not replace its saved payload or reopen its receipt.
    if not existing:
        write_json(path, {'item': item, 'phase': 'pending'})
    elif existing.get('phase') not in ('delivered', 'read', 'legacy-delivered'):
        changed = False
        saved, incoming = existing['item']['answer'], item['answer']
        if not (saved.get('text') or saved.get('outcome')) and (incoming.get('text') or incoming.get('outcome')):
            # Empty payloads fail validation before append. Confirm that no bus
            # publication exists before filling one from a later observation.
            try:
                row_id = publication_id(item)
            except ValueError:
                pass
            else:
                bus.configure(manifest)
                if not any(row.get('id') == row_id for row in bus.read()):
                    existing['item']['answer'] = incoming
                    changed = True
        # Correlation may be registered after the consuming stream retained the
        # response. Enrich missing routing without reopening a delivered receipt.
        try:
            card_subject(existing['item'])
        except ValueError:
            try:
                subject = card_subject(item)
            except ValueError:
                pass
            else:
                existing['item']['subject_ref'] = subject
                changed = True
        if changed:
            write_json(path, existing)
    return path


def queued(manifest, item):
    if not isinstance(item, dict) or not isinstance(item.get('ask_id'), str) or not isinstance(item.get('answer'), dict):
        return False
    try:
        return receipt_path(manifest, item).is_file()
    except ValueError:
        return False


def process(manifest, path, client, legacy_pending=None):
    record = read_json(path)
    if record.get('phase') in ('read', 'invalid', 'legacy-delivered'):
        return record['phase'] != 'invalid'
    item = record['item']
    ask_id = item['ask_id']
    try:
        answer = item['answer']
        if not (answer.get('response_event_id') or answer.get('response_event_ref')):
            # Correlation can arrive after publication and after the source read
            # ack. Recheck retained legacy work even when no new inbox item arrives.
            if read_json(stage(manifest, item)).get('phase') == 'legacy-delivered':
                return True
        response_id = answer_id(item)
        subject, owner, re_id, question = route(manifest, item)
        answer = item['answer']
        text = str(answer.get('text') or answer.get('outcome') or '')
        if not text or len(text.encode()) > 1024 * 1024:
            raise ValueError('answer text missing or too large')
        # A repeated response text is still a distinct operator event. Bind the
        # publication identity to the event, never merely the ask or its text.
        row_id = record.get('bus_id') or publication_id(item)
        # Reuse an ask-only publication once when its response identity arrives.
        # This also recovers an old loop writer that appended its bus row and
        # crashed before unlinking .answer.json: deliver/shared_answer both
        # enter here under the same lock, before publication or exact read ack.
        # A durable binding prevents a second, genuinely distinct event from
        # being swallowed even if its text is identical.
        from .loop_answers import path as loop_ask_path
        legacy = read_json(legacy_path(manifest, ask_id), {})
        legacy_id = 'answer-' + ask_id
        legacy_row = next((r for r in bus.read() if r.get('id') == legacy_id), None)
        if (not record.get('bus_id') and legacy.get('response_event_id') in (None, response_id)
                and (legacy.get('phase') == 'legacy-delivered' or loop_ask_path(manifest, ask_id).is_file())
                and legacy_row
                and delivered(legacy_row, subject, owner, text)):
            row_id = legacy_id
            record_legacy_delivery(manifest, item, owner, row_id, response_id, legacy_pending)
        row = next((r for r in bus.read() if r.get('id') == row_id), None)
        if row is None:
            args = ['post', '--from', 'coordkit', '--to', owner, '--kind', 'decision',
                    '--subject', subject, '--topic', 'anx-answer', '--id', row_id,
                    '--rollback', 'Revise the answer on its original ANX ask.', '--force', '--no-mirror']
            if re_id:
                args += ['--re', re_id]
            with contextlib.redirect_stdout(sys.stderr):
                bus.main([*args, '--', text], manifest)
            row = next((r for r in bus.read() if r.get('id') == row_id), None)
        if not row or not delivered(row, subject, owner, text):
            raise ValueError('answer delivery missing or conflicts with a prior response')
        settle_legacy_receipts(manifest, item, row_id, legacy_pending)
        if question is not None:
            from .questions import accept_anx_delivery
            # Retire the question's default/escalation ladder before suppression
            # or acknowledgement, even when the full inbox cannot be listed.
            accept_anx_delivery(manifest, question['qid'], text, row_id)
        from .loop_answers import path as loop_ask_path
        provenance = loop_ask_path(manifest, ask_id)
        loop_record = read_json(provenance)
        if loop_record is not None:
            loop_record['answer'] = answer
            write_json(provenance, loop_record)
        record.update(phase='delivered', bus_id=row_id)
        write_json(path, record)
        # Acknowledge the exact response, not the latest reply to this ask.
        body = path.with_suffix('.read-body')
        write_json(body, {'answer_event_id': response_id})
        result = client.call('api', 'call', '--method', 'POST', '--path', '/agent-inbox/answers/read',
                             '--from-file', str(body), write=True, timeout=30)
        ack = result.get('body', result).get('answer', {})
        if ack.get('read') is not True or ack.get('answer_event_id') != response_id:
            raise ValueError('answer read acknowledgement was not confirmed')
        record.update(phase='read', read_at=time.time())
        write_json(path, record)
        return True
    except (ToolError, OSError, ValueError, SystemExit, KeyError, TypeError, AttributeError) as exc:
        record['error'] = getattr(exc, 'category', type(exc).__name__)
        write_json(path, record)
        return False


def consume(manifest):
    manifest.writable()
    if not manifest.anx.workspace:
        return
    with delivery_lock(manifest) as acquired:
        if not acquired:
            return
        directory = home(manifest)
        client = AnxClient(manifest)
        status = {'at': time.time(), 'poll_error': None}
        try:
            snapshot = unread(client)
        except (ToolError, ValueError, OSError, TypeError, KeyError, AttributeError) as exc:
            status['poll_error'] = getattr(exc, 'category', type(exc).__name__)
            snapshot = []
        for item in snapshot:
            try:
                if owns(client, item) and item.get('answer') is not None and item.get('answer_unread') is not False:
                    stage(manifest, item)
            except (ValueError, TypeError, KeyError, AttributeError):
                reject(manifest, item)
            except (ToolError, OSError) as exc:
                status['poll_error'] = getattr(exc, 'category', type(exc).__name__)
        bus.configure(manifest)
        pending = legacy_pending(manifest)
        for path in sorted(directory.glob('*.json')):
            if path.name != 'status.json':
                process(manifest, path, client, pending)
        write_json(directory/'status.json', status)


def shared_answer(manifest, item, client=None):
    """Every shared-stream loop relay uses the unread consumer's lock and receipt."""
    manifest.writable()
    with delivery_lock(manifest) as acquired:
        if not acquired:
            return False  # shared-stream callers retain their own durable retry
        client = client or AnxClient(manifest)
        try:
            if not owns(client, item):
                return False
            from .loop_answers import path as loop_ask_path
            loop_record = read_json(loop_ask_path(manifest, reference(item['ask_id'])), {})
            if not item.get('subject_ref') and not item.get('subject') and loop_record:
                item = dict(item, subject_ref=loop_record['card'])
            path = stage(manifest, item)
        except (ValueError, TypeError, KeyError, AttributeError):
            reject(manifest, item)
            return False
        bus.configure(manifest)
        return process(manifest, path, client)


def question_answer(manifest, record, answer):
    """Share the same publication and lock when the question sweep wins first."""
    manifest.writable()
    with delivery_lock(manifest) as acquired:
        if not acquired:
            return False  # owning consumer will settle it; the question remains pending
        item = {'ask_id': reference(record['anx_ask_id']), 'subject_ref': manifest.escalation.card,
                'answer': answer}
        path = stage(manifest, item)
        bus.configure(manifest)
        return process(manifest, path, AnxClient(manifest))


def health(manifest):
    status = read_json(home(manifest)/'status.json', {})
    pending = [read_json(p) for p in home(manifest).glob('*.json') if p.name != 'status.json']
    unresolved = [r for r in pending if r.get('phase') not in ('read', 'legacy-delivered')]
    if status.get('poll_error') or unresolved:
        invalid = sum(r.get('phase') == 'invalid' for r in unresolved)
        return f'WARN unread answer delivery pending={len(unresolved)} invalid={invalid} poll_error={status.get("poll_error")} details={home(manifest)}'
    legacy_rows = {r['bus_id'] for r in pending if r.get('phase') == 'legacy-delivered'}
    confirmed = {r.get('bus_id') for r in pending if r.get('phase') == 'read'}
    if legacy_rows:
        return (f'legacy_local_deliveries={len(legacy_rows)} '
                f'source_read_unconfirmed={len(legacy_rows - confirmed)} details={home(manifest)}')
    return 'requester answers delivered before read acknowledgement' if status else 'not yet polled'
