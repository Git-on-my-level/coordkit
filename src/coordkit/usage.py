"""Read-only, account- and workspace-scoped transcript usage. No harness control."""
from collections import Counter
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import os
from itertools import islice
from pathlib import Path
import shutil
import sqlite3
import tempfile
from .identity import encoded_cwd, transcript_dir

TOKEN_FIELDS = ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')


def _count(value):
    return value if type(value) is int and value >= 0 else 0


def transcript_sources(lane):
    """A session-scoped lane reads only its own transcript and that session's subagent folder."""
    directory = transcript_dir(lane)
    if not lane.session_id:
        return [directory]
    return [directory / f'{lane.session_id}.jsonl', directory / lane.session_id]


def timestamp(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@dataclass
class Usage:
    truncated: bool = False
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    max_context: int = 0
    models: list[str] = field(default_factory=list)
    latest_model: str = ''
    missing_usage: int = 0
    invalid_rows: int = 0
    unreadable_files: int = 0
    # Context tokens at the newest assistant turn in scope, whatever its age: the live size to watch.
    context_now: int = 0
    # Compaction boundaries inside the window, plus the newest one overall.
    compactions: int = 0
    last_compact_at: str = ''
    last_compact_pre: int = 0
    last_compact_post: int = 0


MAX_FILES = 128
MAX_ENTRIES = 2048
TAIL_BYTES = 2 * 1024 * 1024
TOTAL_BYTES = 16 * 1024 * 1024


def tail_lines(path, limit, *, complete=True):
    """Read complete physical rows from a bounded byte tail, dropping a partial prefix."""
    with Path(path).open('rb') as stream:
        size = stream.seek(0, 2)
        offset = max(0, size - limit)
        stream.seek(max(0, offset - 1))
        boundary = not offset or stream.read(1) == b'\n'
        stream.seek(offset)
        data = stream.read(limit)
    lines = data.split(b'\n')
    if complete or not lines[-1]:
        lines = lines[:-1]
    if offset and not boundary:
        lines = lines[1:]
    return [line.decode('utf-8', errors='replace') for line in lines]


def jsonl_paths(source, *, status=None):
    """Bound traversal work as well as returned files; never follow directory symlinks."""
    path = Path(source)
    if path.is_file():
        return [path]
    paths, pending, remaining = [], [(path, 0)], MAX_ENTRIES
    while pending and remaining > 0 and len(paths) < MAX_FILES:
        directory, depth = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in islice(entries, remaining):
                    remaining -= 1
                    if entry.is_file(follow_symlinks=False) and entry.name.endswith('.jsonl'):
                        paths.append(Path(entry.path))
                        if len(paths) >= MAX_FILES:
                            break
                    elif entry.is_dir(follow_symlinks=False):
                        if depth < 8:
                            pending.append((Path(entry.path), depth + 1))
                        elif status is not None:
                            status['truncated'] = True
        except OSError:
            continue
    if status is not None and (pending or remaining == 0 or len(paths) >= MAX_FILES):
        status['truncated'] = True
    return paths


def parse_transcripts(sources, since, until):
    """Aggregate unique assistant messages in [since, until], including nested subagents.

    Sources are directories or single transcript files.
    Repeated streaming rows merge counters by maximum, retaining the latest model.
    Missing usage still counts as a turn, with an explicit incompleteness counter.
    No message content is retained. Inputs and time bounds are explicit for fixtures.
    """
    messages, invalid, unreadable = {}, 0, 0
    compacts = []
    status = {}
    selected = list(islice(sources, MAX_FILES + 1))
    paths = sorted({p for source in selected[:MAX_FILES] for p in jsonl_paths(source, status=status)})
    truncated = bool(status.get('truncated')) or len(paths) > MAX_FILES or len(selected) > MAX_FILES
    remaining = TOTAL_BYTES
    for path in paths[:MAX_FILES]:
        try:
            limit = min(TAIL_BYTES, remaining)
            if limit <= 0:
                truncated = True
                break
            size = path.stat().st_size
            truncated |= size > limit
            remaining -= min(size, limit)
            for line_number, line in enumerate(tail_lines(path, limit, complete=False)):
                try:
                    row = json.loads(line)
                except ValueError:
                    invalid += 1
                    continue
                if isinstance(row, dict) and row.get('type') == 'system' and row.get('subtype') == 'compact_boundary':
                    meta = row.get('compactMetadata') if isinstance(row.get('compactMetadata'), dict) else {}
                    stamp = timestamp(row.get('timestamp'))
                    if stamp is not None:
                        compacts.append((stamp, _count(meta.get('preTokens')), _count(meta.get('postTokens'))))
                    continue
                if not isinstance(row, dict) or row.get('type') != 'assistant':
                    continue
                msg = row.get('message')
                if not isinstance(msg, dict):
                    continue
                at = timestamp(row.get('timestamp'))
                if at is None:
                    invalid += 1
                    continue
                # IDs are unique within a scanned lane scope, across parent and subagent files.
                key = msg.get('id') or row.get('uuid') or (str(path), line_number)
                if not isinstance(key, (str, tuple)):
                    invalid += 1
                    continue
                usage = msg.get('usage')
                valid = isinstance(usage, dict) and any(k in usage for k in TOKEN_FIELDS)
                counters = {k: v if type(v := (usage or {}).get(k)) is int and v >= 0 else 0
                            for k in TOKEN_FIELDS} if isinstance(usage, dict) else dict.fromkeys(TOKEN_FIELDS, 0)
                model = msg.get('model') if isinstance(msg.get('model'), str) else ''
                if key not in messages:
                    messages[key] = dict(at=at, model=model, usage=counters, valid=valid)
                else:
                    previous = messages[key]
                    for k in TOKEN_FIELDS:
                        previous['usage'][k] = max(previous['usage'][k], counters[k])
                    previous['valid'] |= valid
                    if at >= previous['at']:
                        previous.update(at=at, model=model or previous['model'])
        except OSError:
            unreadable += 1
    result = Usage(invalid_rows=invalid, unreadable_files=unreadable, truncated=truncated)
    ordered = sorted(messages.values(), key=lambda item: item['at'])
    if ordered:
        newest = ordered[-1]['usage']
        result.context_now = sum(newest[k] for k in TOKEN_FIELDS if k != 'output_tokens')
    for stamp, pre, post in sorted(compacts):
        if since <= stamp <= until:
            result.compactions += 1
        result.last_compact_at, result.last_compact_pre, result.last_compact_post = stamp.isoformat(), pre, post
    for item in ordered:
        if not since <= item['at'] <= until:
            continue
        result.turns += 1
        result.missing_usage += not item['valid']
        for k, count in item['usage'].items():
            setattr(result, k, getattr(result, k) + count)
        result.max_context = max(result.max_context, sum(item['usage'][k] for k in TOKEN_FIELDS if k != 'output_tokens'))
        if item['model']:
            result.latest_model = item['model']
            if item['model'] not in result.models:
                result.models.append(item['model'])
    return result


OPENCODE_DB = 'opencode.db'
OPENCODE_MAX_SESSIONS = 512
OPENCODE_MAX_MESSAGES = 20000
OPENCODE_TAIL = 20000


def lane_harness(lane):
    """Explicit [lanes].harness, with an OpenCode session id as a safe fallback."""
    explicit = str(getattr(lane, 'harness', '') or '').lower()
    if explicit in ('opencode', 'hermes'):
        return explicit
    if explicit in ('claude', 'claude-code'):
        return 'claude'
    return 'opencode' if str(getattr(lane, 'session_id', '') or '').startswith('ses_') else 'claude'


def opencode_store(lane):
    """OpenCode's read-only session store; config_dir is its data directory."""
    path = Path(lane.config_dir).expanduser()
    return path if path.name == OPENCODE_DB else path / OPENCODE_DB


def _epoch_millis(value):
    if type(value) not in (int, float) or not value >= 0:
        return None
    return value / 1000 if value >= 10 ** 11 else value


def _opencode_model(data):
    model = data.get('model')
    if isinstance(model, dict):
        return model.get('id') or model.get('modelID') or ''
    if isinstance(model, str):
        return model
    identifier = data.get('modelID') or ''
    return identifier


def _opencode_counters(data):
    tokens = data.get('tokens')
    if not isinstance(tokens, dict):
        tokens = data.get('usage')
    if not isinstance(tokens, dict):
        return None
    cache = tokens.get('cache') if isinstance(tokens.get('cache'), dict) else {}
    return {'input_tokens': _count(tokens.get('input')),
            'output_tokens': _count(tokens.get('output')),
            'cache_read_input_tokens': _count(cache.get('read', tokens.get('cache_read'))),
            'cache_creation_input_tokens': _count(cache.get('write', tokens.get('cache_write')))}


OPENCODE_SNAPSHOT_ATTEMPTS = 4


def _stat_key(path):
    """Identity, size and mtime of a live store file, or None when it is absent."""
    try:
        info = Path(path).stat()
    except OSError:
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _copy_opencode_store(path, copy):
    """Copy the store plus its WAL and rollback journal.

    A vanished sidecar simply means the store has none. The rollback journal
    matters for non-WAL stores: a copy taken mid-commit holds partially written
    pages, and SQLite only rolls them back on open if the hot journal travels
    with the copy (the private directory is writable, so the rollback happens
    there, never beside the live store).
    """
    sidecars = [(Path(str(path) + suffix), Path(str(copy) + suffix)) for suffix in ('-wal', '-journal')]
    before = (_stat_key(path),) + tuple(_stat_key(source) for source, _ in sidecars)
    shutil.copyfile(path, copy)
    for source, target in sidecars:
        try:
            shutil.copyfile(source, target)
        except FileNotFoundError:
            # A concurrent checkpoint or commit may remove the sidecar.
            target.unlink(missing_ok=True)
    return before == (_stat_key(path),) + tuple(_stat_key(source) for source, _ in sidecars)


@contextlib.contextmanager
def _opencode_snapshot(path):
    """Yield a private copy of the live OpenCode store; never open it in place.

    `immutable=1` is wrong for a live store: committed pages can still sit in
    the WAL, so bypassing WAL recovery returns stale counters. Opening the
    store read-only from its own directory is also unsafe: SQLite may create
    `-shm`/`-wal` sidecars even for a read-only connection, and the journal
    mode can change between a header check and connect. The copy is therefore
    unconditional, and a private directory is removed on every path, including
    failed copies.

    Copying a database and WAL is not atomic with the writer. Each attempt
    compares the source files before and after copying and retries when a
    concurrent checkpoint moved pages between them, so a snapshot cannot
    silently combine an old database with an emptied WAL.
    """
    directory = Path(tempfile.mkdtemp(prefix='coordkit-opencode-'))
    try:
        copy = directory / OPENCODE_DB
        for _ in range(OPENCODE_SNAPSHOT_ATTEMPTS):
            if _copy_opencode_store(path, copy):
                break
        else:
            raise OSError('opencode store changed while it was being snapshotted')
        yield copy
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _opencode_connect(path):
    """Open the caller's private snapshot copy, never the live store.

    The copy is opened read-write on purpose: a copied hot rollback journal (or
    WAL) must be recovered, and SQLite writes during that recovery; `mode=ro`
    fails with SQLITE_READONLY_ROLLBACK instead. Everything it writes stays in
    the private snapshot directory. `query_only` still refuses every
    statement-level write, so coordkit itself never changes the data.
    """
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=rw', uri=True, timeout=0.25)
    connection.execute('PRAGMA query_only = ON')
    return connection


def _opencode_tables(connection):
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _opencode_sessions(connection, tables, lane):
    """Sessions in this lane's store, scoped by session id or workspace directory.

    Upgraded OpenCode stores keep a migrated session in both session_v2 and
    session. v2 is the copy still being written, so a v1 id already found there
    is not a second session.
    """
    if not {'session', 'session_v2'} & tables:
        return [], False
    directories = {str(lane.cwd)}
    try:
        directories.add(os.path.realpath(str(lane.cwd)))
    except OSError:
        pass
    marks = ','.join('?' for _ in directories)
    found, truncated, seen = [], False, set()
    for table in ('session_v2', 'session'):
        if table not in tables:
            continue
        if lane.session_id:
            rows = connection.execute(f'SELECT id FROM {table} WHERE id = ? LIMIT 2',
                                      (lane.session_id,)).fetchall()
        else:
            rows = connection.execute(f'SELECT id FROM {table} WHERE directory IN ({marks}) LIMIT ?',
                                      (*directories, OPENCODE_MAX_SESSIONS + 1)).fetchall()
        truncated |= len(rows) > OPENCODE_MAX_SESSIONS
        for (identifier,) in rows[:OPENCODE_MAX_SESSIONS]:
            if identifier in seen:
                continue
            seen.add(identifier)
            found.append((table, identifier))
    return found[:OPENCODE_MAX_SESSIONS * 2], truncated


def _read_opencode(connection, lane, path, *, since=None, until=None):
    tables = _opencode_tables(connection)
    sessions, truncated = _opencode_sessions(connection, tables, lane)
    messages, invalid, unreadable = {}, 0, 0
    total = 0
    for table, session_id in sessions:
        if total >= OPENCODE_MAX_MESSAGES:
            truncated = True
            break
        source = 'session_message' if table == 'session_v2' else 'message'
        if source not in tables:
            continue
        columns = 'id, data, time_created'
        sql = f'SELECT {columns} FROM {source} WHERE session_id = ?'
        if source == 'session_message':
            sql += " AND type = 'assistant'"
        sql += ' ORDER BY time_created DESC LIMIT ?'
        try:
            rows = connection.execute(sql, (session_id, min(OPENCODE_TAIL, OPENCODE_MAX_MESSAGES - total) + 1)).fetchall()
        except sqlite3.Error:
            unreadable += 1
            continue
        truncated |= len(rows) > OPENCODE_TAIL
        for identifier, raw, created in rows[:OPENCODE_TAIL]:
            if total >= OPENCODE_MAX_MESSAGES:
                truncated = True
                break
            total += 1
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                invalid += 1
                continue
            if not isinstance(data, dict):
                invalid += 1
                continue
            if source == 'message' and data.get('role') != 'assistant':
                continue
            stamp = _epoch_millis((data.get('time') or {}).get('created')
                                  if isinstance(data.get('time'), dict) else None)
            if stamp is None:
                stamp = _epoch_millis(created)
            counters = _opencode_counters(data)
            model = _opencode_model(data)
            if stamp is None:
                invalid += 1
                continue
            try:
                at = datetime.fromtimestamp(stamp, timezone.utc)
            except (OverflowError, OSError, ValueError):
                invalid += 1
                continue
            if since is not None and at < since or until is not None and at > until:
                continue
            key = identifier if isinstance(identifier, str) else (str(path), identifier)
            messages[key] = dict(at=at, model=model, usage=counters or dict.fromkeys(TOKEN_FIELDS, 0),
                                 valid=counters is not None)
    ordered = sorted(messages.values(), key=lambda item: item['at'])
    return ordered, truncated, invalid, unreadable


def _opencode_messages(lane, *, since=None, until=None):
    """Assistant message counters from OpenCode v1/v2 SQLite rows; never writes."""
    try:
        path = opencode_store(lane)
    except (OSError, ValueError, TypeError):
        return [], False, 0, 1
    try:
        with _opencode_snapshot(path) as source:
            connection = _opencode_connect(source)
            try:
                return _read_opencode(connection, lane, path, since=since, until=until)
            finally:
                connection.close()
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return [], False, 0, 1


def opencode_usage(lane, since, until):
    ordered, truncated, invalid, unreadable = _opencode_messages(lane)
    result = Usage(truncated=truncated or len(ordered) >= OPENCODE_MAX_MESSAGES,
                   invalid_rows=invalid, unreadable_files=unreadable)
    if ordered:
        newest = ordered[-1]['usage']
        result.context_now = sum(newest[k] for k in TOKEN_FIELDS if k != 'output_tokens')
    for item in ordered[:OPENCODE_MAX_MESSAGES]:
        if not since <= item['at'] <= until:
            continue
        result.turns += 1
        result.missing_usage += not item['valid']
        for key, count in item['usage'].items():
            setattr(result, key, getattr(result, key) + count)
        context = sum(item['usage'][k] for k in TOKEN_FIELDS if k != 'output_tokens')
        result.max_context = max(result.max_context, context)
        if item['model']:
            result.latest_model = item['model']
            if item['model'] not in result.models:
                result.models.append(item['model'])
    return result


def opencode_observed_model(lane):
    """Newest assistant model from the local OpenCode store, bounded to the recent tail."""
    ordered, _, _, _ = _opencode_messages(lane)
    for item in reversed(ordered):
        if item['model']:
            return item['model'], item['at'].isoformat()
    return None, None


def hermes_usage(lane, since, until):
    """Sum the per-turn reports of turns that ended in [since, until]. Turns count provider calls.

    Hermes reports usage per run, not per call, so context size is unknown here.
    Turns started outside coordkit are not in the receipts and are not counted.
    """
    from .adapters import hermes
    rows, invalid, unreadable = hermes.receipts(lane)
    result = Usage(invalid_rows=invalid, unreadable_files=unreadable)
    for row in rows:
        at = datetime.fromtimestamp(row['ended_at'], timezone.utc)
        if not since <= at <= until:
            continue
        usage = row.get('usage') or {}
        calls = _count(usage.get('api_calls'))
        result.turns += calls or 1
        result.missing_usage += not calls
        for key in TOKEN_FIELDS:
            setattr(result, key, getattr(result, key) + _count(usage.get(key)))
        model = row.get('model')
        if isinstance(model, str) and model:
            result.latest_model = model
            if model not in result.models:
                result.models.append(model)
    return result


def hermes_observed_model(lane):
    from .adapters import hermes
    rows, _, _ = hermes.receipts(lane)
    for row in reversed(rows):
        if isinstance(row.get('model'), str) and row['model']:
            return row['model'], datetime.fromtimestamp(row['ended_at'], timezone.utc).isoformat()
    return None, None


def scope_key(lane):
    if lane_harness(lane) == 'opencode':
        return ('opencode', str(opencode_store(lane)), str(lane.cwd))
    if lane_harness(lane) == 'hermes':
        return ('hermes', str(lane.config_dir), lane.name)
    return ('claude', str(transcript_dir(lane)))


def read_lane(lane, since, until):
    if lane_harness(lane) == 'opencode':
        return opencode_usage(lane, since, until)
    if lane_harness(lane) == 'hermes':
        return hermes_usage(lane, since, until)
    return parse_transcripts(transcript_sources(lane), since, until)


def model_matches(intended, actual):
    """A short family alias accepts its versioned variants; a version stays pinned.

    A provider-qualified OpenCode id (`provider/model`) matches an unqualified
    model, but two different providers never match just because their model
    names share a last component.
    """
    intended, actual = (v.lower().removeprefix('claude-') for v in (intended, actual))
    intended_provider, intended_sep, intended_name = intended.rpartition('/')
    actual_provider, actual_sep, actual_name = actual.rpartition('/')
    if intended_sep and actual_sep and intended_provider != actual_provider:
        return False
    intended = intended_name if intended_sep else intended
    actual = actual_name if actual_sep else actual
    return intended == actual or (intended.isalpha() and actual.startswith(intended + '-'))


def follows_profile(lane):
    """A hermes lane with model = "default" runs whatever its profile configures."""
    return lane_harness(lane) == 'hermes' and str(lane.model).lower() == 'default'


def drift(lane, usage):
    if follows_profile(lane):
        return []
    return [model for model in usage.models if not model_matches(lane.model, model)]


def collect(lanes, at):
    """Session-scoped lanes are always attributable. Others need a unique account/project pair.

    Session-scoped lanes still count toward the pair: an unscoped lane sharing their directory
    would otherwise read their transcripts as its own.
    """
    counts = Counter(scope_key(lane) for lane in lanes)
    return [(lane, None if not lane.session_id and counts[scope_key(lane)] > 1 and lane_harness(lane) != 'hermes' else
             read_lane(lane, at - timedelta(hours=1), at)) for lane in lanes]


def doctor_warnings(manifest, at=None):
    at = at or datetime.now(timezone.utc)
    warnings = []
    for lane, usage in collect(manifest.lanes, at):
        if usage is None:
            warnings.append(f'lane={lane.name} ambiguous account/cwd; no attribution')
        elif drift(lane, usage):
            warnings.append(f'lane={lane.name} intended={lane.model} actual={",".join(drift(lane, usage))}')
    return warnings


MODEL_TAIL_BYTES = 128 * 1024


def observed_model(sources):
    """Last assistant model in a bounded tail of the newest transcript; no content retained."""
    candidates = []
    for source in sources:
        for path in jsonl_paths(source):
            try:
                candidates.append((path.stat().st_mtime, str(path), path))
            except OSError:
                continue
    if not candidates:
        return None, None
    path = max(candidates)[2]
    try:
        with path.open('rb') as stream:
            stream.seek(0, 2)
            offset = max(0, stream.tell() - MODEL_TAIL_BYTES)
            stream.seek(offset)
            data = stream.read(MODEL_TAIL_BYTES)
        lines = data.splitlines()
        if offset:
            lines = lines[1:]  # a partial leading JSON row is never evidence
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(row, dict) or row.get('type') != 'assistant':
                continue
            message = row.get('message')
            if isinstance(message, dict) and isinstance(message.get('model'), str) and message['model']:
                at = timestamp(row.get('timestamp'))
                return message['model'], at.isoformat() if at else None
    except OSError:
        pass
    return None, None
