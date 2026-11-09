"""Incremental physical-line reader; integer cursors survive malformed rows.

Checkpoints are hints, never authority. Replaced/truncated logs and stale checkpoints
fall back to a byte scan. Only complete newline-terminated records are consumed.
"""
import hashlib
import os
from pathlib import Path
from .state import read_json, write_json


def read_tail(stream, max_bytes):
    """(first byte offset, complete physical lines) using at most max_bytes + 1 bytes.

    Inspect the preceding byte instead of readline(): even a giant unterminated
    physical line cannot cause an unbounded read. The caller owns any file lock.
    """
    stream.seek(0, os.SEEK_END)
    end = stream.tell()
    start = max(0, end - max_bytes)
    if start:
        stream.seek(start - 1)
        boundary = stream.read(1) == b'\n'
    else:
        boundary = True
        stream.seek(0)
    data = stream.read(max_bytes)
    if not boundary:
        skip = data.find(b'\n') + 1
        if not skip:
            return end, b''
        start += skip
        data = data[skip:]
    return start, data[:data.rfind(b'\n') + 1]


class TailLimit(ValueError):
    """A bounded checkpoint catch-up needs more bus history than its caller permits."""


class TailReader:
    def __init__(self, path, checkpoint=None):
        self.path = Path(path)
        self.checkpoint = checkpoint
        self.state = None
        self.reset = False

    def read(self, start=0, max_bytes=None):
        try:
            stream = self.path.open('rb')
        except FileNotFoundError:
            return 0, []
        with stream:
            stat = os.fstat(stream.fileno())
            identity = [stat.st_dev, stat.st_ino]
            prior = self.state
            if prior is None and self.checkpoint:
                try:
                    prior = read_json(self.checkpoint, {})
                except (OSError, ValueError):
                    prior = {}
            prior = prior if isinstance(prior, dict) else {}
            offset, count = prior.get('offset', 0), prior.get('count', 0)
            valid = (prior.get('file') == identity and type(offset) is int and type(count) is int
                     and 0 <= offset <= stat.st_size and 0 <= count <= start)
            if valid and offset:
                stream.seek(max(0, offset - 128))
                valid = hashlib.sha256(stream.read(min(offset, 128))).hexdigest() == prior.get('boundary')
            self.reset = bool(prior) and not valid
            if not valid:
                offset, count = 0, 0
            stream.seek(offset)
            rows = []
            remaining = max_bytes
            while True:
                line = stream.readline() if remaining is None else stream.readline(remaining + 1)
                if remaining is not None:
                    if len(line) > remaining:
                        raise TailLimit('bus checkpoint catch-up exceeds byte budget')
                    remaining -= len(line)
                if not line or not line.endswith(b'\n'):
                    break
                if count >= start:
                    rows.append((count, line.decode('utf-8', errors='replace').rstrip('\n')))
                offset += len(line)
                count += 1
            stream.seek(max(0, offset - 128))
            boundary = hashlib.sha256(stream.read(min(offset, 128))).hexdigest()
        self.state = dict(file=identity, offset=offset, count=count, boundary=boundary)
        if self.checkpoint:
            write_json(self.checkpoint, self.state)
        return count, rows
