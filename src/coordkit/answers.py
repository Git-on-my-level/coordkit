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


class DeliverySweep:
    """One lock-owned snapshot and pending index shared by every relay in a pass."""
    def __init__(self, manifest):
        bus.configure(manifest)
        self.refresh()
        self.paths = set(home(manifest).glob('*.json'))
        self.records = {path: read_json(path, {}) for path in self.paths}
        self.pending = legacy_pending(manifest, self.records)

    def refresh(self):
        # The writer lock's publication check is still authoritative. Refresh
        # only after append, including a writer that appended and then raised.
        self.rows = bus.read()
        self.by_id = {row.get('id'): row for row in self.rows}
        self.by_ask = {}
        for row in self.rows:
            row_id = row.get('id', '')
            if not isinstance(row_id, str) or not row_id.startswith('answer-event:'):
                continue
            ask = row_id.removeprefix('answer-')
            self.by_ask.setdefault(ask, []).append(row)
            if ':legacy:' in ask:
                self.by_ask.setdefault(ask.split(':legacy:', 1)[0], []).append(row)
            elif re.search(r'-[0-9a-f]{16}$', ask):
                base = ask.rsplit('-', 1)[0]
                self.by_ask.setdefault(base, []).append(row)


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


def route(manifest, item, publication=None, sweep=None, fallback=None):
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
    rows = sweep.rows if sweep is not None else bus.read()
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
    owner = manifest.escalation.internal_to or manifest.obligations.coordinator or fallback
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


def legacy_route(manifest, item, publication=None, sweep=None):
    from .loop_answers import path as loop_ask_path
    loop_record = read_json(loop_ask_path(manifest, reference(item['ask_id'])), {})
    if loop_record and not (item.get('subject_ref') or item.get('subject')):
        item = dict(item, subject_ref=loop_record['card'])
    return route(manifest, item, publication, sweep, 'lead' if loop_record else None)


def legacy_path(manifest, ask_id, row_id=None):
    key = reference(ask_id) if row_id in (None, 'answer-' + reference(ask_id)) else row_id
    return home(manifest)/('legacy-' + hashlib.sha256(key.encode()).hexdigest() + '.json')


def legacy_match(manifest, item, owner=None, sweep=None):
    """Find a verified publication for a response-less loop representation."""
    ask_id = reference(item['ask_id'])
    text = str(item['answer'].get('text') or item['answer'].get('outcome') or '')
    prefix = 'answer-' + ask_id
    rows = sweep.by_ask.get(ask_id, []) if sweep is not None else bus.read()
    for row in reversed(rows):
        row_id = row.get('id', '')
        if not isinstance(row_id, str):
            continue
        if (row_id == prefix or re.fullmatch(re.escape(prefix) + r'(?:-|:legacy:)[0-9a-f]{16}', row_id)):
            try:
                if owner is None:
                    subject, target, _, _ = legacy_route(manifest, item, row, sweep)
                else:
                    subject, target = card_subject(item), owner
            except ValueError:
                continue
            if delivered(row, subject, target, text):
                return row_id
    return None


def record_legacy_delivery(manifest, item, owner, row_id, response_id=None, pending=None, sweep=None):
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
    if row_id == 'answer-' + ask_id or row_id.startswith('answer-' + ask_id + ':legacy:'):
        binding_path = legacy_path(manifest, ask_id, row_id)
        previous = read_json(binding_path, {})
        binding = previous.get('response_event_id') or response_id
        if binding:
            record['response_event_id'] = binding
        write_json(binding_path, record)
    write_json(receipt_path(manifest, local), record)
    settle_legacy_receipts(manifest, item, row_id, pending, sweep)


def legacy_pending(manifest, records=None):
    """Index response-less pending receipts once per sweep, rather than per reply."""
    pending = {}
    records = ({path: read_json(path, {}) for path in home(manifest).glob('*.json')}
               if records is None else records)
    for path, record in records.items():
        item = record.get('item')
        if record.get('phase') != 'pending' or not isinstance(item, dict):
            continue
        answer = item.get('answer')
        if not isinstance(answer, dict) or answer.get('response_event_id') or answer.get('response_event_ref'):
            continue
        key = (item.get('ask_id'), str(answer.get('text') or answer.get('outcome') or ''))
        pending.setdefault(key, []).append(path)
    return pending


def settle_legacy_receipts(manifest, item, row_id, pending=None, sweep=None):
    ask_id = reference(item['ask_id'])
    text = str(item['answer'].get('text') or item['answer'].get('outcome') or '')
    pending = sweep.pending if sweep is not None else pending
    paths = home(manifest).glob('*.json') if pending is None else pending.pop((ask_id, text), [])
    if not paths:
        return
    row = sweep.by_id.get(row_id) if sweep is not None else next((r for r in bus.read() if r.get('id') == row_id), None)
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
                subject, owner, _, _ = legacy_route(manifest, payload, row, sweep)
            except ValueError:
                if pending is not None:
                    pending.setdefault((ask_id, text), []).append(path)
                continue
            if delivered(row, subject, owner, text):
                queued.update(phase='legacy-delivered', bus_id=row_id, source_read=False)
                write_json(path, queued)
            elif pending is not None:
                pending.setdefault((ask_id, text), []).append(path)


def stage(manifest, item, sweep=None):
    item = dict(item, ask_id=reference(item['ask_id']))
    if isinstance(item.get('answer'), str):
        item['answer'] = {'text': item['answer']}
    path = receipt_path(manifest, item)
    existing = read_json(path, {})
    # An unchanged response-less item can remain unread forever. Its durable
    # terminal receipt makes subsequent staging O(1), without any bus lookup.
    if existing.get('phase') in ('delivered', 'read', 'legacy-delivered'):
        return path
    answer = item['answer']
    text = str(answer.get('text') or answer.get('outcome') or '')
    if not text.strip() or len(text.encode()) > 1024 * 1024:
        raise ValueError('answer text missing or too large')
    sweep = sweep if sweep is not None else DeliverySweep(manifest)
    sweep.paths.add(path)
    # This path identifies one immutable response event. Different CLI shapes
    # of that event must not replace its saved payload or reopen its receipt.
    if not existing:
        write_json(path, {'item': item, 'phase': 'pending'})
        if not (answer.get('response_event_id') or answer.get('response_event_ref')):
            sweep.pending.setdefault((item['ask_id'], text), []).append(path)
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
                if row_id not in sweep.by_id:
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


def process(manifest, path, client, legacy_pending=None, sweep=None):
    record = read_json(path)
    if record.get('phase') in ('read', 'invalid', 'legacy-delivered'):
        return record['phase'] != 'invalid'
    item = record['item']
    ask_id = item['ask_id']
    try:
        sweep = sweep if sweep is not None else DeliverySweep(manifest)
        answer = item['answer']
        if not str(answer.get('text') or answer.get('outcome') or '').strip():
            reject(manifest, item)
            path.unlink(missing_ok=True)  # full evidence now lives in invalid-*.json
            return False
        identified = bool(answer.get('response_event_id') or answer.get('response_event_ref'))
        response_id = answer_id(item) if identified else None
        prior_id = record.get('bus_id') or (publication_id(item) if identified else None)
        subject, owner, re_id, question = legacy_route(manifest, item, sweep.by_id.get(prior_id), sweep)
        answer = item['answer']
        text = str(answer.get('text') or answer.get('outcome') or '')
        if not text.strip() or len(text.encode()) > 1024 * 1024:
            raise ValueError('answer text missing or too large')
        # A repeated response text is still a distinct operator event. Bind the
        # publication identity to the event, never merely the ask or its text.
        row_id = record.get('bus_id')
        if not row_id:
            if identified:
                row_id = publication_id(item)
            else:
                row_id = legacy_match(manifest, item, sweep=sweep)
                if row_id:
                    subject, owner, re_id, question = legacy_route(manifest, item, sweep.by_id[row_id], sweep)
                else:
                    row_id = 'answer-' + ask_id
                if row_id in sweep.by_id and not delivered(sweep.by_id[row_id], subject, owner, text):
                    # A revised legacy ruling has no event identity. Keep its
                    # content and destination distinct while retries converge.
                    identity = json.dumps([subject, bus.resolve(owner), re_id, text])
                    row_id += ':legacy:' + hashlib.sha256(identity.encode()).hexdigest()[:16]
        # Reuse an ask-only publication once when its response identity arrives.
        # This also recovers an old loop writer that appended its bus row and
        # crashed before unlinking .answer.json: deliver/shared_answer both
        # enter here under the same lock, before publication or exact read ack.
        # A durable binding prevents a second, genuinely distinct event from
        # being swallowed even if its text is identical.
        from .loop_answers import path as loop_ask_path
        legacy_id = 'answer-' + ask_id
        if identified and not record.get('bus_id'):
            for legacy_row in reversed(sweep.by_ask.get(ask_id, [])):
                candidate = legacy_row['id']
                if candidate != legacy_id and not candidate.startswith(legacy_id + ':legacy:'):
                    continue
                legacy = read_json(legacy_path(manifest, ask_id, candidate), {})
                candidate_subject, candidate_owner, candidate_re, candidate_question = legacy_route(manifest, item, legacy_row, sweep)
                if (legacy.get('response_event_id') in (None, response_id)
                        and (legacy.get('phase') == 'legacy-delivered'
                             or loop_ask_path(manifest, ask_id).is_file()
                             or sweep.pending.get((ask_id, text)))
                        and delivered(legacy_row, candidate_subject, candidate_owner, text)):
                    subject, owner, re_id, question = candidate_subject, candidate_owner, candidate_re, candidate_question
                    row_id = candidate
                    record_legacy_delivery(manifest, dict(item, subject_ref=subject), owner,
                                           row_id, response_id, legacy_pending, sweep)
                    break
        row = sweep.by_id.get(row_id)
        if row is None:
            args = ['post', '--from', 'coordkit', '--to', owner, '--kind', 'decision',
                    '--subject', subject, '--topic', 'anx-answer', '--id', row_id,
                    '--rollback', 'Revise the answer on its original ANX ask.', '--force', '--no-mirror']
            if re_id:
                args += ['--re', re_id]
            try:
                with contextlib.redirect_stdout(sys.stderr):
                    bus.main([*args, '--', text], manifest)
            finally:
                sweep.refresh()
            row = sweep.by_id.get(row_id)
        if not row or not delivered(row, subject, owner, text):
            raise ValueError('answer delivery missing or conflicts with a prior response')
        settle_legacy_receipts(manifest, item, row_id, legacy_pending, sweep)
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
        if not identified:
            record_legacy_delivery(manifest, dict(item, subject_ref=subject), owner,
                                   row_id, pending=legacy_pending, sweep=sweep)
            record.update(phase='legacy-delivered', bus_id=row_id, source_read=False)
            write_json(path, record)
            return True
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
        sweep = DeliverySweep(manifest)
        for item in snapshot:
            try:
                if owns(client, item) and item.get('answer') is not None and item.get('answer_unread') is not False:
                    stage(manifest, item, sweep)
            except (ValueError, TypeError, KeyError, AttributeError):
                reject(manifest, item)
            except (ToolError, OSError) as exc:
                status['poll_error'] = getattr(exc, 'category', type(exc).__name__)
        for path in sorted(sweep.paths):
            if path.name != 'status.json' and sweep.records.get(path, {}).get('phase') not in ('read', 'legacy-delivered', 'invalid'):
                process(manifest, path, client, sweep=sweep)
        write_json(directory/'status.json', status)


def shared_answer(manifest, item, client=None, sweep=None):
    """Every shared-stream loop relay uses the unread consumer's lock and receipt."""
    manifest.writable()
    if sweep is None:
        with delivery_lock(manifest) as acquired:
            if not acquired:
                return False  # shared-stream callers retain their own durable retry
            return shared_answer(manifest, item, client, DeliverySweep(manifest))
    # Supplying a sweep is internal: its caller already owns consume.lock.
    client = client or AnxClient(manifest)
    try:
        if not owns(client, item):
            return False
        from .loop_answers import path as loop_ask_path
        loop_record = read_json(loop_ask_path(manifest, reference(item['ask_id'])), {})
        if not item.get('subject_ref') and not item.get('subject') and loop_record:
            item = dict(item, subject_ref=loop_record['card'])
        path = stage(manifest, item, sweep)
    except (ValueError, TypeError, KeyError, AttributeError):
        reject(manifest, item)
        return False
    return process(manifest, path, client, sweep=sweep)


def question_answer(manifest, record, answer):
    """Share the same publication and lock when the question sweep wins first."""
    manifest.writable()
    with delivery_lock(manifest) as acquired:
        if not acquired:
            return False  # owning consumer will settle it; the question remains pending
        item = {'ask_id': reference(record['anx_ask_id']), 'subject_ref': manifest.escalation.card,
                'answer': answer}
        return shared_answer(manifest, item, sweep=DeliverySweep(manifest))


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
