"""Synchronous tmux answer adapter used by questions.deliver.

Never types over nonempty input. Pickup requires an agent reply or transcript growth;
failure records confirmation evidence and uses the addressed fallback and relay.
The tmux binary is $TMUX_BIN or tmux on PATH. There is no sidecar CLI or tick loop.
"""
import os
import re
import subprocess
import time

from . import addressed
from .addressed import cfg, escalate, registry, seconds

DEFAULT_PROMPT = r'^\s*[│|]?\s*(?:>|❯|›)\s?(?P<text>.*?)\s*[│|]?\s*$'


def tmux(*args, timeout=10):
    argv = [os.environ.get('TMUX_BIN', 'tmux'), *args]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'tmux {args[0]} rc={result.returncode}: {result.stderr.strip()[:200]}')
    return result.stdout


def input_state(capture, prompt=DEFAULT_PROMPT):
    """('empty', ''), ('busy', text) or ('unknown', '') from the bottom of a pane capture."""
    pattern = re.compile(prompt)
    lines = [line for line in capture.splitlines() if line.strip()][-8:]
    for line in reversed(lines):
        match = pattern.match(line)
        if match:
            text = (match.groupdict().get('text') or '').strip()
            return ('busy', text) if text else ('empty', '')
    return ('unknown', '')


def pointer(row, name):
    text = ' '.join((row.get('text') or '').split())[:160]
    text = ''.join(ch for ch in text if ch.isprintable())
    level = 'URGENT ' if row.get('priority') == 'urgent' else ''
    return (f"[bus] {level}{row.get('orig_kind') or row['kind']} {row['id']} from {row['frm']}: {text} "
            f"-- reply on the bus with --re {row['id']} as {name}")


def transcript_size(path):
    try:
        return os.stat(path).st_size
    except (OSError, TypeError):
        return None


def confirmed(manifest, name, row_id, transcript, before, deadline, clock=time.time, sleep=time.sleep, rows=None):
    from . import bus
    while True:
        if any(addressed.agent_reply(r, name, row_id) for r in (bus.read() if rows is None else rows)):
            return 'ack'
        size = transcript_size(transcript)
        if before is not None and size is not None and size > before:
            return 'transcript'
        if clock() >= deadline:
            return None
        sleep(min(1.0, max(0.0, deadline - clock())))


def delivery_failed(manifest, name, row, lease, event, why, out=print, escalate_busy=True):
    """Shared fallback for synchronous and tick-driven confirmation."""
    urgent = row.get('priority') == 'urgent' or addressed.priority(row) == 'urgent'
    fallback = registry(manifest).get(name, {}).get('fallback', '')
    if event == 'delivery_unconfirmed':
        pending = list(dict.fromkeys([*(lease.data.get('unconfirmed') or []), row['id']]))[-50:]
        stamps = dict(lease.data.get('unconfirmed_at') or {})
        stamps[row['id']] = lease.clock()
        lease.renew(unconfirmed=pending, unconfirmed_at={i: stamps[i] for i in pending if i in stamps})
    text = (f"{event.upper()} {name} {row.get('orig_kind') or row['kind']} {row['id']} from {row['frm']} "
            f"({why}): {(row.get('text') or '')[:160]}")
    if event != 'input_busy' or escalate_busy:
        out(text)
        escalate(manifest, event, text, row, deaf=[name], fallback=fallback, urgent=urgent,
                 post=manifest.mode == 'live')
    return event


def deliver(manifest, name, target, row, lease, transcript=None, prompt=DEFAULT_PROMPT,
            clock=time.time, sleep=time.sleep, out=print, escalate_busy=True):
    """Deliver a question answer once and confirm pickup."""
    def fail(event, why):
        return delivery_failed(manifest, name, row, lease, event, why, out, escalate_busy)

    from . import bus
    if any(addressed.agent_reply(r, name, row['id']) for r in bus.read()):
        return 'confirmed:ack'  # already picked up (e.g. via inbox); nothing to type
    try:
        state, held = input_state(tmux('capture-pane', '-p', '-t', target), prompt)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        return fail('delivery_unconfirmed', f'capture failed: {exc}')
    if state != 'empty':
        why = f'input box holds text {held[:40]!r}; not typing over it' if state == 'busy' else 'no input line found'
        return fail('input_busy', why)
    before = transcript_size(transcript)
    line = pointer(row, name)
    try:
        tmux('send-keys', '-t', target, '-l', line)
        # Re-read before Enter: if anything besides our pointer is now on the input line (a human
        # started typing in the gap), do not submit it. The pane must be agent-owned (humans attach
        # read-only, `tmux attach -r`); this check narrows, but cannot close, the remaining race.
        state, held = input_state(tmux('capture-pane', '-p', '-t', target), prompt)
        if state != 'busy' or held != line.strip():
            return fail('delivery_unconfirmed', f'input line changed while typing ({held[:40]!r}); Enter not sent')
        tmux('send-keys', '-t', target, 'Enter')
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        return fail('delivery_unconfirmed', f'send-keys failed: {exc}')
    how = confirmed(manifest, name, row['id'], transcript, before,
                    clock() + seconds(cfg(manifest).confirm_timeout), clock, sleep)
    if how is None:
        return fail('delivery_unconfirmed', f'no ack or transcript growth within {cfg(manifest).confirm_timeout}')
    out(f'DELIVERY_CONFIRMED {name} {row["id"]} via {how}')
    return 'confirmed:' + how
