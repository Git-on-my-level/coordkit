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


def route(manifest, item):
    from .questions import load_all
    ask_id = reference(item['ask_id'])
    subject = item.get('subject_ref') or (item.get('subject') or {}).get('ref')
    if not isinstance(subject, str) or not re.fullmatch(r'card:[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', subject):
        raise ValueError('answer has no valid card subject')
    # Exact question correlation retains its original asker and reply edge, even
    # if an agent already closed it before the human answered.
    for record in load_all(manifest).values():
        if record.get('anx_ask_id') and reference(record['anx_ask_id']) == ask_id:
            return subject, record['asker'], record['qid']
    rows = bus.read()
    from .briefs import blocker_title
    blockers = [r for r in bus.open_blockers(rows) if r.get('subject') == subject
                and item.get('title') in (blocker_title(r), ('Coordkit: ' + r['text'])[:99].rstrip('.?') + '?')]
    if len(blockers) == 1:
        return subject, blockers[0]['frm'], blockers[0]['id']
    # A card-level answer is still work for the configured coordinator. Never
    # infer permission to execute its text or choose one of ambiguous blockers.
    owner = manifest.escalation.internal_to or manifest.obligations.coordinator
    if not owner:
        raise ValueError('answer has no configured coordinating lane')
    return subject, owner, None


def answer_id(item):
    answer = item['answer']
    value = answer.get('response_event_id') or answer.get('response_event_ref')
    return reference(value).removeprefix('event:')


def receipt_path(manifest, item):
    # Distinct responses to one ask keep distinct receipts, including a newer
    # reply arriving while delivery of the previous response is still pending.
    answer = item.get('answer') or {}
    response = answer.get('response_event_id') or answer.get('response_event_ref')
    if isinstance(response, str):
        response = response.removeprefix('event:')
    key = json.dumps([reference(item['ask_id']), response or answer], sort_keys=True)
    return home(manifest)/(hashlib.sha256(key.encode()).hexdigest() + '.json')


def already_delivered(manifest, item):
    if not isinstance(item, dict) or not isinstance(item.get('ask_id'), str) or not isinstance(item.get('answer'), dict):
        return False
    record = read_json(receipt_path(manifest, item), {})
    return record.get('phase') in ('delivered', 'read')


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


def consume(manifest):
    manifest.writable()
    if not manifest.anx.workspace:
        return
    directory = home(manifest)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory/'consume.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        client = AnxClient(manifest)
        status = {'at': time.time(), 'pending': [], 'poll_error': None}
        try:
            items = unread(client)
            for item in items:
                ask_id = reference(item.get('ask_id'))
                if not isinstance(item.get('answer'), dict) or item.get('answer_unread') is False:
                    continue
                item = dict(item, ask_id=ask_id)
                path = receipt_path(manifest, item)
                existing = read_json(path, {})
                if existing.get('phase') == 'read' or existing.get('item', {}).get('answer') != item['answer']:
                    write_json(path, {'item': dict(item, ask_id=ask_id), 'phase': 'pending'})
        except (ToolError, ValueError, OSError, TypeError, KeyError, AttributeError) as exc:
            status['poll_error'] = getattr(exc, 'category', type(exc).__name__)
        # Retained payloads also retry if the remote answer listing is unavailable.
        bus.configure(manifest)
        for path in sorted(directory.glob('*.json')):
            if path.name == 'status.json':
                continue
            record = read_json(path)
            if record.get('phase') == 'read':
                continue
            item = record['item']
            ask_id = item['ask_id']
            try:
                response_id = answer_id(item)
                subject, owner, re_id = route(manifest, item)
                answer = item['answer']
                text = str(answer.get('text') or answer.get('outcome') or '')
                if not text or len(text.encode()) > 1024 * 1024:
                    raise ValueError('answer text missing or too large')
                row_id = 'answer-' + ask_id
                row = next((r for r in bus.read() if r.get('id') == row_id), None)
                if row and not delivered(row, subject, owner, text):
                    row_id += '-' + hashlib.sha256(response_id.encode()).hexdigest()[:16]
                    row = next((r for r in bus.read() if r.get('id') == row_id), None)
                if row is None:
                    args = ['post', '--from', 'coordkit', '--to', owner, '--kind', 'decision',
                            '--subject', subject, '--topic', 'anx-answer', '--id', row_id,
                            '--rollback', 'Revise the answer on its original ANX ask.', '--force', '--no-mirror']
                    if re_id:
                        args += ['--re', re_id]
                    with contextlib.redirect_stdout(sys.stderr):
                        bus.main([*args, text], manifest)
                    row = next((r for r in bus.read() if r.get('id') == row_id), None)
                if not row or not delivered(row, subject, owner, text):
                    raise ValueError('answer delivery missing or conflicts with a prior response')
                record.update(phase='delivered', bus_id=row_id)
                write_json(path, record)
                # Use the exact response event from the requester-scoped snapshot.
                # `inbox read ASK` re-resolves the latest response and could hide
                # a newer answer that arrived after our snapshot/delivery.
                body = path.with_suffix('.read-body')
                write_json(body, {'answer_event_id': response_id})
                result = client.call('api', 'call', '--method', 'POST', '--path', '/agent-inbox/answers/read',
                            '--from-file', str(body), write=True, timeout=30)
                ack = result.get('body', result).get('answer', {})
                if ack.get('read') is not True or ack.get('answer_event_id') != response_id:
                    raise ValueError('answer read acknowledgement was not confirmed')
                record.update(phase='read', read_at=time.time())
                write_json(path, record)
            except (ToolError, OSError, ValueError, SystemExit, KeyError, TypeError, AttributeError) as exc:
                record['error'] = getattr(exc, 'category', type(exc).__name__)
                write_json(path, record)
                status['pending'].append(ask_id)
        write_json(directory/'status.json', status)


def health(manifest):
    status = read_json(home(manifest)/'status.json', {})
    pending = [read_json(p) for p in home(manifest).glob('*.json') if p.name != 'status.json']
    unresolved = [r for r in pending if r.get('phase') != 'read']
    if status.get('poll_error') or unresolved:
        return f'WARN unread answer delivery pending={len(unresolved)} poll_error={status.get("poll_error")} details={home(manifest)}'
    return 'requester answers delivered before read acknowledgement' if status else 'not yet polled'
