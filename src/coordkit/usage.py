"""Read-only, account- and workspace-scoped transcript usage. No harness control."""
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re

TOKEN_FIELDS = ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')


def _count(value):
    return value if type(value) is int and value >= 0 else 0


def encoded_cwd(cwd):
    """The harness project key replaces non-ASCII-alphanumeric path characters."""
    return re.sub(r'[^a-zA-Z0-9]', '-', str(cwd))


def transcript_dir(lane):
    return Path(lane.config_dir).expanduser() / 'projects' / encoded_cwd(lane.cwd)


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


def jsonl_paths(source):
    path = Path(source)
    if path.is_file():
        return [path]
    return list(path.rglob('*.jsonl')) if path.is_dir() else []


def parse_transcripts(sources, since, until):
    """Aggregate unique assistant messages in [since, until], including nested subagents.

    Sources are directories or single transcript files.
    Repeated streaming rows merge counters by maximum, retaining the latest model.
    Missing usage still counts as a turn, with an explicit incompleteness counter.
    No message content is retained. Inputs and time bounds are explicit for fixtures.
    """
    messages, invalid, unreadable = {}, 0, 0
    compacts = []
    paths = sorted({p for source in sources for p in jsonl_paths(source)})
    for path in paths:
        try:
            with path.open(encoding='utf-8', errors='replace') as stream:
                for line_number, line in enumerate(stream):
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
    result = Usage(invalid_rows=invalid, unreadable_files=unreadable)
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


def model_matches(intended, actual):
    """A short family alias accepts its versioned variants; a version stays pinned."""
    intended, actual = (v.lower().removeprefix('claude-') for v in (intended, actual))
    return intended == actual or (intended.isalpha() and actual.startswith(intended + '-'))


def drift(lane, usage):
    return [model for model in usage.models if not model_matches(lane.model, model)]


def collect(lanes, at):
    """Session-scoped lanes are always attributable. Others need a unique account/project pair.

    Session-scoped lanes still count toward the pair: an unscoped lane sharing their directory
    would otherwise read their transcripts as its own.
    """
    counts = Counter(str(transcript_dir(lane)) for lane in lanes)
    return [(lane, None if not lane.session_id and counts[str(transcript_dir(lane))] > 1 else
             parse_transcripts(transcript_sources(lane), at - timedelta(hours=1), at)) for lane in lanes]


def doctor_warnings(manifest, at=None):
    at = at or datetime.now(timezone.utc)
    warnings = []
    for lane, usage in collect(manifest.lanes, at):
        if usage is None:
            warnings.append(f'lane={lane.name} ambiguous account/cwd; no attribution')
        elif drift(lane, usage):
            warnings.append(f'lane={lane.name} intended={lane.model} actual={",".join(drift(lane, usage))}')
    return warnings
