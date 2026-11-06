"""Optional forward-offset transcript input, never used to identify a lead.

Extracted from the auditor: only words and tool descriptions, never tool results.
Offsets end on complete lines; a saved boundary must not skip the next line.
"""
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys

FIRST_RUN_BYTES = 12_000_000
MAX_CHARS = 90_000
REDACT = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
    (re.compile(r"\b(?:sk|ghp|gho|xox[a-z]|ya29|AIza|hf)[-_.][\w-]{10,}"), "[secret]"),
    (re.compile(r"\beyJ[\w-]{10,}\.[\w-]{10,}(?:\.[\w-]+)?"), "[secret]"),
    (re.compile(r"\b(?=[\w+/-]*\d)(?=[\w+/-]*[A-Za-z])[\w+/-]{41,}={0,2}"), "[long-token]"),
]
REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def clean(text):
    text = REMINDER.sub("", text)
    for pattern, replacement in REDACT:
        text = pattern.sub(replacement, text)
    return text.strip()


def lines_from(entry):
    kind = entry.get("type")
    if kind not in ("user", "assistant"):
        return
    content = (entry.get("message") or {}).get("content")
    stamp = (entry.get("timestamp") or "")[11:16]
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    for block in content or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = clean(block.get("text") or "")
            if text:
                who = "HUMAN_OR_SYSTEM" if kind == "user" else "LEAD"
                yield f"[{stamp}Z {who}] {text[:6000]}"
        elif block.get("type") == "tool_use":
            description = ((block.get("input") or {}).get("description") or "")[:160]
            if description:
                yield f"[{stamp}Z LEAD_RAN] {clean(description)}"


@dataclass
class Extract:
    text: str
    offset: int
    identity: list[int]


def extract(transcript: Path, checkpoint: Path) -> Extract:
    stat = transcript.stat()
    identity = [stat.st_dev, stat.st_ino]
    saved = json.loads(checkpoint.read_text()) if checkpoint.exists() else None
    fresh = saved is None or saved['identity'] != identity or saved['offset'] > stat.st_size
    offset = max(0, stat.st_size - FIRST_RUN_BYTES) if fresh else saved['offset']
    collected = []
    with transcript.open('rb') as stream:
        stream.seek(offset)
        if fresh and offset:
            stream.readline()  # only the first bounded read may start in the middle of a line
            offset = stream.tell()
        while True:
            start = stream.tell()
            raw = stream.readline()
            if not raw or not raw.endswith(b'\n'):
                offset = start
                break
            offset = stream.tell()
            try:
                entry = json.loads(raw)
            except ValueError as exc:
                print(f'EXTRACT_INVALID_JSON offset={start}: {exc}', file=sys.stderr)
                continue
            collected.extend(lines_from(entry))
            # Retain bounded text even for huge appends.
            if sum(map(len, collected)) > MAX_CHARS * 2:
                collected = ['[older text this cycle omitted]', '\n'.join(collected)[-MAX_CHARS:]]
    text = '\n'.join(collected)
    if len(text) > MAX_CHARS:
        text = '[older text this cycle omitted]\n' + text[-MAX_CHARS:]
    return Extract(text, offset, identity)


def commit(checkpoint: Path, extracted: Extract):
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    temporary = checkpoint.with_suffix('.tmp')
    temporary.write_text(json.dumps({'offset': extracted.offset, 'identity': extracted.identity}) + '\n')
    temporary.replace(checkpoint)
