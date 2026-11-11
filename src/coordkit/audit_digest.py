"""Bounded, secret-scrubbed transcript input for a lane state audit."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from .errors import ManifestError

MAX_DIGEST = 1024 * 1024  # about a quarter of a million tokens: one audit covers a long session
MAX_READ = 2 * 1024 * 1024
MAX_MESSAGES = 4096
ARG_CHARS = 1000
SECRET_PATTERNS = (
    r'(?i)\b[a-z][a-z0-9+.-]*://[^\s/<>"\']+@',
    r'-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----[\s\S]*?(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|\Z)',
    r'(?im)\bAuthorization\s*:[^\r\n]*',
    r'''(?ix)\b[\w.-]*(?:api[_-]?key|key|token|secret|password)["']?\s*[=:]\s*(?:"[^"\n]*"|'[^'\n]*'|[^\s,;}]+)''',
    r'\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,})\b',
    r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b',
    r'(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*',
)


def scrub(text):
    """Remove overlapping matches once, before any truncation can split a secret."""
    spans = sorted(match.span() for pattern in SECRET_PATTERNS for match in re.finditer(pattern, text))
    merged = []
    for start, end in spans:
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    for start, end in reversed(merged):
        text = text[:start] + '[REDACTED]' + text[end:]
    return text, len(merged)


def locate(lane):
    from .usage import lane_harness
    if lane_harness(lane) == 'hermes':
        from .adapters.hermes import session_id
        session = session_id(lane)
        if not session:
            raise ManifestError('audit_transcript_missing: no remembered Hermes session')
        return Path(lane.config_dir)/'state.db', session
    if lane_harness(lane) != 'claude':
        raise ManifestError('audit_transcript_unsupported: use --transcript with JSONL')
    from .identity import transcript_dir
    directory = transcript_dir(lane)
    if lane.session_id:
        return directory/f'{lane.session_id}.jsonl', ''
    paths = list(directory.glob('*.jsonl'))
    if not paths:
        raise ManifestError('audit_transcript_missing: no Claude transcript')
    return max(paths, key=lambda path: path.stat().st_mtime_ns), ''


def jsonl_rows(path, prior):
    with path.open('rb') as stream:
        stat = os.fstat(stream.fileno())
        identity = [str(path.resolve()), stat.st_dev, stat.st_ino]
        offset = prior.get('offset', 0)
        valid = (prior.get('file') == identity and type(offset) is int and 0 <= offset <= stat.st_size)
        if valid and offset:
            stream.seek(max(0, offset - 128))
            valid = hashlib.sha256(stream.read(min(128, offset))).hexdigest() == prior.get('boundary')
        start = offset if valid else 0
        seek = max(start, stat.st_size - MAX_READ)
        stream.seek(max(0, seek - 1))
        boundary = not seek or stream.read(1) == b'\n'
        stream.seek(seek)
        if not boundary:
            fragment = stream.readline(MAX_READ)  # a partial leading record is never parsed
            if not fragment.endswith(b'\n'):
                # A huge incomplete final record must not become a consumed cursor.
                stream.seek(max(0, start - 128))
                digest = hashlib.sha256(stream.read(min(128, start))).hexdigest()
                return [], {'file': identity, 'offset': start, 'boundary': digest}, True
        begin = stream.tell()
        data = stream.read(max(0, MAX_READ - (begin - seek)))
        complete = data.rfind(b'\n') + 1
        if not complete and begin == stat.st_size and begin > start:
            raise ManifestError('audit_transcript_record_too_large: newest complete JSONL record exceeds read bound')
        end = begin + complete
        stream.seek(max(0, end - 128))
        digest = hashlib.sha256(stream.read(min(128, end))).hexdigest()
    rows = []
    for line in data[:complete].splitlines():
        if line.strip():
            try:
                row = json.loads(line)
            except ValueError:
                raise ManifestError('audit_transcript_invalid: malformed complete JSONL record') from None
            if not isinstance(row, dict):
                raise ManifestError('audit_transcript_invalid: expected JSONL objects')
            rows.append(row)
    return rows, {'file': identity, 'offset': end, 'boundary': digest}, begin > start


def sqlite_rows(path, session, prior):
    # mode=ro neither creates a missing store nor changes its journal mode.
    with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as connection:
        connection.execute('PRAGMA query_only = ON')
        identity = [str(path.resolve()), session, path.stat().st_ino]
        start = prior.get('message_id', 0) if prior.get('file') == identity else 0
        if type(start) is not int or start < 0:
            start = 0
        maximum = connection.execute('SELECT COALESCE(MAX(id), 0) FROM messages WHERE session_id = ?', (session,)).fetchone()[0]
        if start > maximum:
            start = 0
        records = connection.execute(
            'SELECT id, role, substr(content, 1, ?), substr(tool_calls, 1, ?), timestamp, '
            'length(content), length(tool_calls) '
            'FROM messages WHERE session_id = ? AND id > ? ORDER BY id DESC LIMIT ?',
            (MAX_READ, MAX_READ, session, start, MAX_MESSAGES + 1))
        rows, size, truncated = [], 0, False
        for index, (ident, role, content, tools, stamp, content_size, tool_size) in enumerate(records):
            size += len((content or '').encode()) + len((tools or '').encode())
            if index >= MAX_MESSAGES or size > MAX_READ or max(content_size or 0, tool_size or 0) > MAX_READ:
                if not rows:
                    raise ManifestError('audit_transcript_record_too_large: newest message exceeds read bound')
                truncated = True
                break
            try:
                calls = json.loads(tools) if tools else []
            except ValueError:
                raise ManifestError('audit_transcript_invalid: malformed tool calls') from None
            rows.append({'role': role, 'content': content, 'tool_calls': calls, 'timestamp': stamp})
    return list(reversed(rows)), {'file': identity, 'message_id': maximum}, truncated


def render(row):
    message = row.get('message', row)
    if not isinstance(message, dict):
        return '', 0
    role = message.get('role', row.get('type', ''))
    if role not in ('user', 'assistant'):
        return '', 0
    parts, removed = [], 0
    content = message.get('content', '')
    blocks = content if isinstance(content, list) else [{'type': 'text', 'text': content}]
    calls = message.get('tool_calls') or []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get('type') == 'text' and isinstance(block.get('text'), str):
            clean, count = scrub(block['text'])
            parts.append(clean)
            removed += count
        elif block.get('type') == 'tool_use':
            calls = [*calls, {'function': {'name': block.get('name'), 'arguments': block.get('input')}}]
    for call in calls:
        if not isinstance(call, dict):
            continue
        function = call.get('function', call)
        if not isinstance(function, dict):
            continue
        args = function.get('arguments', '')
        clean, count = scrub(args if isinstance(args, str) else json.dumps(args, ensure_ascii=False))
        removed += count
        name, count = scrub(str(function.get('name', '?')))
        removed += count
        parts.append(f'Tool {name[:100]}: {clean[:ARG_CHARS]}')
    stamp, count = scrub(str(row.get('timestamp', '')))
    return (f'{stamp[:80]} {role}: ' + '\n'.join(parts) + '\n' if parts else ''), removed + count


def build(lane, prior=None, transcript=None, carry=()):
    prior = prior or {}
    path, session = (Path(transcript).expanduser(), '') if transcript else locate(lane)
    rows, cursor, truncated = sqlite_rows(path, session, prior) if session else jsonl_rows(path, prior)
    parts, removed = [], 0
    if carry:
        clean, count = scrub('Prior audit gaps to reconcile against current state and later transcript:\n' + json.dumps(carry, ensure_ascii=False) + '\n')
        parts.append(clean)
        removed += count
    for row in rows:
        text, count = render(row)
        parts.append(text)
        removed += count
    # Prior gaps stay whole in front. Newest transcript fills only the bytes left over.
    prefix = parts[0].encode() if carry else b''
    tail = b''.join(part.encode() for part in (parts[1:] if carry else parts))
    truncated |= len(prefix) + len(tail) > MAX_DIGEST - 256
    header = (f'Transcript digest. Secret spans removed: {removed}. '
              f'Older content omitted by bounds: {str(truncated).lower()}.\n')
    budget = MAX_DIGEST - len(header.encode())
    room = budget - len(prefix)
    if room < 0:
        raise ManifestError('audit_carry_too_large: prior gaps must fit whole in the digest')
    body = prefix + tail[-room:] if room else prefix
    text = header + body.decode('utf-8', errors='ignore')
    return text, cursor, {'digest_bytes': len(text.encode()), 'removed_spans': removed, 'truncated': truncated}
