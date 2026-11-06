"""Auditor output parsing and bounded reports; last MISSED_JSON block wins."""
import json
import re
from .manifest import ManifestError
from .obligations import flag_id

BLOCK = re.compile(r'MISSED_JSON_START\s*(\{.*?\})\s*MISSED_JSON_END', re.S)
BUS_CAP = 380


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def find_items(raw):
    try:
        candidates = list(strings(json.loads(raw)))
    except ValueError:
        candidates = [raw]  # Plain content is the ordinary agentctl --content result.
    for text in reversed(candidates):
        for match in reversed(BLOCK.findall(text)):
            try:
                parsed = json.loads(match)
            except ValueError as exc:
                raise ManifestError(f'POST_INVALID_BLOCK: {exc}') from exc
            items = parsed.get('items') if isinstance(parsed, dict) else None
            if not isinstance(items, list):
                raise ManifestError('POST_INVALID_ITEMS: expected items list')
            # Preserve the original <=5 titled items discipline.
            return [item for item in items if isinstance(item, dict) and isinstance(item.get('title'), str)
                    and item['title'].strip()][:5]
    raise ManifestError('POST_NO_BLOCK')


def prior_line(item):
    return f"- [{item.get('severity')}/{item.get('kind')}] {item['title']} | {item.get('next_action', '')}"


def new_items(items, prior):
    """Compare findings to saved prior.md, including evidence when a structured receipt exists.

    Older prior files contain only the human-readable severity/kind/title/action line.
    Accept those lines as the baseline without requiring a noisy one-time repost.
    """
    saved = {}
    legacy = set(prior.splitlines())
    for line in prior.splitlines():
        if line.startswith('<!-- finding ') and line.endswith(' -->'):
            try:
                item = json.loads(line[len('<!-- finding '):-len(' -->')])
                saved[flag_id(item['title'])] = item
            except (ValueError, KeyError, TypeError):
                continue
    changed = []
    seen = set()
    for item in items:
        key = flag_id(item['title'])
        previous = saved.get(key)
        same = previous == item if previous is not None else prior_line(item) in legacy
        signature = json.dumps(item, sort_keys=True)
        if not same and signature not in seen:
            changed.append(item)
            seen.add(signature)
    return changed


def report(cycle, items):
    lines = [f'# Missed-work audit {cycle}', '']
    for item in items:
        lines.extend([f"## [{item.get('severity')}/{item.get('kind')}] {flag_id(item['title'])} {item['title']}",
                      f"Evidence: {item.get('evidence', '')}", '', f"Next: {item.get('next_action', '')}", ''])
    if not items:
        lines.append('Nothing raised.')
    return '\n'.join(lines) + '\n'


def message(items, findings):
    ids = ' '.join(flag_id(item['title']) for item in items)
    summary = f'{len(items)} new finding(s) {ids}; findings.md: {findings}'
    if len(summary) > BUS_CAP:
        summary = f'{len(items)} new finding(s) {ids}; findings.md (see evidence)'
    return summary
