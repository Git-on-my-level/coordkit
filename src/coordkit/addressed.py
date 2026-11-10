"""Addressed bus routing, listening evidence, receipt state, and bounded sweep alerts.

Listening accepts recent delivery hooks, explicitly bound busy transcripts, legacy leases,
and the optional incumbent bus-wait process probe. Human roles never require a waiter.
Live escalations reach a declared fallback or relay and the relay file; lead escalations
can batch into an hourly digest. Shadow sweeps write only local state and relay output.
"""
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time

from .manifest import ManifestError, duration
from . import obligations
from .obligations import priority
from .process import parse_ts
from .state import name_key, read_json, write_json

WAKE_KINDS = ('ask', 'handoff', 'ruling', 'fail', 'apply_request', 'blocker', 'decision')
ACK_STATES = ('received', 'done', 'blocked')
EXIT_FINDINGS, EXIT_DEAF, EXIT_VACANT = 5, 7, 8
SHELLS = {'sh', 'bash', 'zsh', 'dash', 'fish', 'login', 'sudo', 'env', 'timeout', 'perl', 'nohup'}
REARMS = ('delivered', 'timeout')  # clean exits after which the adapter re-arms


def cfg(manifest):
    return manifest.addressed


def home(manifest):
    return manifest.paths.root/'state'/'addressed'


def seconds(value):
    return duration(value)


# -- registry ---------------------------------------------------------------------------------------

from .routing import registry, roles


def is_human(manifest, name):
    """Recognize the question operator, human agents, roles and their members."""
    if name and name == manifest.questions.human_actor:
        return True
    spec = registry(manifest).get(name, {})
    if spec.get('human', False):
        return True
    for role, spec in cfg(manifest).roles.items():
        if isinstance(spec, dict) and spec.get('human', False) and (name == role or name in roles(manifest)[role]):
            return True
    return False


def digest_only(manifest, name):
    """Humans, leads and relays receive one digest, never item reminders."""
    return (is_human(manifest, name)
            or name in {cfg(manifest).relay, manifest.obligations.coordinator}
            or (name in lead_names(manifest) and not cfg(manifest).lead_escalations))


def known_names(manifest):
    return set(registry(manifest)) | set(roles(manifest))


def target_kind(manifest, to):
    """'role', 'name' (registered), or None (not addressed by this layer)."""
    if to in roles(manifest):
        return 'role'
    return 'name' if to in registry(manifest) else None


from .routing import locked


# -- leases -----------------------------------------------------------------------------------------

def lease_path(manifest, name):
    return home(manifest)/'leases'/f'{name_key(name)}.json'


def read_lease(manifest, name):
    return read_json(lease_path(manifest, name))


def incumbent_waiters():
    """Names with a running incumbent `bus wait --to NAME` process on this host."""
    try:
        out = subprocess.run(['ps', '-axo', 'args='], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {name for line in out.splitlines() if (name := incumbent_listener(line))}


def incumbent_listener(args):
    """NAME if this process is the incumbent bus waiting for NAME, else None.

    The process itself must be the bus (an interpreter running a script named `bus`, or `bus`
    directly) with `wait` as its subcommand. Shell wrappers whose command string merely contains
    `bus wait --to NAME` never count (they can outlive the bus process).
    """
    import shlex
    try:
        argv = shlex.split(args)
    except ValueError:
        return None
    if not argv or os.path.basename(argv[0]).lstrip('-') in SHELLS:
        return None
    for i, token in enumerate(argv[:3]):
        if os.path.basename(token) == 'bus':
            rest = argv[i+1:]
            break
    else:
        return None
    if not rest or rest[0] != 'wait' or i > 0 and not re.search(r'python', os.path.basename(argv[0]), re.I):
        return None
    for j, token in enumerate(rest):
        if token == '--to' and j + 1 < len(rest):
            return rest[j+1]
        if token.startswith('--to='):
            return token[5:]
    return None


PROBE = incumbent_waiters  # tests replace this


def listening(manifest, name, at=None, probe=None, rows=None, *, waiter_only=False):
    """(live, evidence). Busy sessions can hear hooks without an armed waiter."""
    at = time.time() if at is None else at
    from .wake import retired
    retirement = retired(manifest, name)
    if retirement:
        return False, 'retired; redirect to ' + retirement.get('owner', 'lead')
    if waiter_only:
        from .routing import waiter_evidence
        return waiter_evidence(manifest, name, at)
    if is_human(manifest, name):
        return True, 'human recipient'
    lease = read_lease(manifest, name)
    if lease:
        open_ids = unconfirmed(manifest, name, lease, rows, at=at)
        if not open_ids and not lease.get('ended_at') and lease['heartbeat_at'] + seconds(cfg(manifest).lease_ttl) >= at:
            return True, f'lease renewed {int(at - lease["heartbeat_at"])}s ago'
        if not open_ids and lease.get('ended_at') and lease.get('end_reason') in REARMS \
                and lease['ended_at'] + seconds(cfg(manifest).rearm_grace) >= at:
            return True, f'waiter exited ({lease.get("end_reason")}) {int(at - lease["ended_at"])}s ago; re-arm grace'
    evidence = activity_evidence(manifest, name, at)
    if evidence:
        return True, evidence
    from .routing import waiter_evidence
    waiting = waiter_evidence(manifest, name, at)
    if waiting[0]:
        return waiting
    if rows is None:
        from . import bus
        rows = bus.read_path(manifest.paths.bus_log)
    latest = None
    from .routing import local_activity
    for row in rows:
        # Synthetic question rows, connector posts and relay-applied traffic
        # cannot prove that the sender is a local listener.
        if row.get('frm') != name or not local_activity(row):
            continue
        try:
            posted = parse_ts(row['ts']).timestamp()
        except (ValueError, TypeError, KeyError):
            continue
        if 0 <= at - posted <= seconds(cfg(manifest).activity_ttl):
            latest = max(latest or posted, posted)
    # MCP service posts carry no evidence that an agent session can hear us.
    if latest is not None and name != manifest.mcp.identity:
        return True, f'posted {int(at - latest)}s ago'
    if lease and open_ids:
        return False, f'delivery_unconfirmed {",".join(open_ids[:3])}'
    if cfg(manifest).incumbent_waiters and name in (probe or PROBE)():
        return True, 'incumbent bus wait process (pre-cutover probe)'
    if not lease:
        return False, 'no waiter lease'
    last = lease.get('ended_at') or lease['heartbeat_at']
    return False, f'waiter lease lapsed {int((at - last) // 60)}m ago ({lease.get("end_reason") or "not renewed"})'


def activity_evidence(manifest, name, at):
    """Stat only transcripts with an explicit session/name binding; never guess by cwd.

    Hooks save their observed transcript. Existing `question bind` records identify
    session files under the configured harness directories, and pinned usage lanes
    provide an exact path. Broken/missing optional evidence preserves lease behavior.
    """
    from pathlib import Path
    from . import questions
    from .usage import transcript_dir
    try:
        note = read_json(home(manifest)/'hooks'/f'{name_key(name)}.json', {}) or {}
        age = at - note.get('at', 0)
        if note.get('name') == name and 0 <= age <= seconds(cfg(manifest).lease_ttl):
            return f'hook delivery {int(age)}s ago'
    except (OSError, ValueError, TypeError, AttributeError):
        note = {}
    try:
        sessions = read_json(questions.home(manifest)/'sessions.json', {}) or {}
        paths = []
        session = note.get('session_id')
        if note.get('name') == name and note.get('transcript_path') and (
                session not in sessions or sessions[session].get('name') == name):
            paths.append(Path(note['transcript_path']))
        for session, binding in sessions.items():
            if binding.get('name') != name:
                continue
            if binding.get('transcript_path'):
                paths.append(Path(binding['transcript_path']).expanduser())
            # Bind's session is a filename, not a glob or a path supplied by the caller.
            if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', session):
                for directory in manifest.questions.config_dirs:
                    paths.extend((Path(directory).expanduser()/'projects').glob(f'*/{session}.jsonl'))
        paths.extend(transcript_dir(lane)/f'{lane.session_id}.jsonl' for lane in manifest.lanes
                     if lane.name == name and lane.session_id and (
                         lane.session_id not in sessions or sessions[lane.session_id].get('name') == name))
        newest = None
        for path in set(paths):
            try:
                stat = path.stat()
                if path.is_file() and (newest is None or stat.st_mtime > newest):
                    newest = stat.st_mtime
            except OSError:
                continue
        if newest is not None and 0 <= at - newest <= seconds(cfg(manifest).busy_window):
            return f'busy: transcript grew {int(at - newest)}s ago'
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def unconfirmed(manifest, name, lease, rows=None, at=None):
    """Unacked sidecar deliveries within confirm_timeout; old failures cannot latch deafness."""
    at = time.time() if at is None else at
    pending = lease.get('unconfirmed') or []
    if not pending:
        return []
    if rows is None:
        from . import bus
        rows = bus.read_path(manifest.paths.bus_log)
    acked = {r.get('re') for r in rows if agent_reply(r, name, r.get('re'))}
    row_times = {r['id']: parse_ts(r['ts']).timestamp() for r in rows if r.get('id') in pending and r.get('ts')}
    stamps = lease.get('unconfirmed_at') or {}
    fallback = lease.get('started_at', lease.get('heartbeat_at', 0))
    return [i for i in pending if i not in acked and
            at - stamps.get(i, row_times.get(i, fallback)) < seconds(cfg(manifest).confirm_timeout)]


def first_live(manifest, role, at=None, probe=None, skip=()):
    for member in roles(manifest).get(role, []):
        if member not in skip and listening(manifest, member, at, probe)[0]:
            return member
    return None


def resolve_live(manifest, target, at=None, probe=None, skip=()):
    """A live name for a fallback that is a name or a role, else None."""
    if not target or target in skip:
        return None
    if target in roles(manifest):
        return first_live(manifest, target, at, probe, skip)
    return target if listening(manifest, target, at, probe)[0] else None


# -- escalation -------------------------------------------------------------------------


def relay_file(manifest):
    return manifest.path(cfg(manifest).relay_file) if cfg(manifest).relay_file else None


def to_relay_file(manifest, text, at=None):
    path = relay_file(manifest)
    if path is None:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(json.dumps({'at': time.time() if at is None else at, 'text': text}) + '\n')
        fcntl.flock(stream, fcntl.LOCK_UN)
    return True


DIGEST_EVERY = 3600


def lead_names(manifest):
    """Configured role names and members, plus the built-in lead."""
    out = {'lead'}
    for role in roles(manifest):
        out.add(role)
        out.update(roles(manifest).get(role, []))
    return out


def to_lead_digest(manifest, event, text, row=None, at=None):
    path = home(manifest)/'lead-digest.ndjson'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        items = digest_items(stream)
        if event == 'orphan-backlog':
            items = [i for i in items if i.get('event') != event]
        item = {'at': time.time() if at is None else at, 'event': event, 'text': text[:400],
                'id': (row or {}).get('id')}
        if any(all(old.get(k) == item[k] for k in ('event', 'text', 'id')) for old in items):
            return
        stream.seek(0); stream.truncate()
        stream.write(b''.join((json.dumps(i) + '\n').encode() for i in [*items[-999:], item]))
        stream.flush(); os.fsync(stream.fileno())
        fcntl.flock(stream, fcntl.LOCK_UN)


def digest_items(stream):
    """Bounded queue tail; one malformed finding never blocks the hourly batch."""
    from .tail import read_tail
    _, data = read_tail(stream, 262144)
    items = []
    for line in data.splitlines():
        try:
            item = json.loads(line)
            if isinstance(item, dict) and isinstance(item.get('text'), str):
                items.append(item)
        except (ValueError, RecursionError):
            continue
    return items[-1000:]


def flush_lead_digest(manifest, state, at, out=print, backlog=(), pending=(), dead=(), rows=None):
    """One hourly FYI; consume the shared queue under the same lock writers take."""
    if at - state.get('lead_digest_at', 0) < DIGEST_EVERY:
        return
    from . import approvals, questions, machine
    machine_lines = machine.lines(manifest)
    rows = questions.recent_rows(manifest) if rows is None else rows
    questions.reconcile_defaults(manifest, rows, at)
    opened = questions.open_questions(manifest, at)
    unanswered = [r for r in opened if r.get('unanswered_no_default') and not r.get('no_default_digested')]
    deferred = [r for r in opened if r.get('default_deferred') and not r.get('default_deferred_digested')][:12]
    missing = approvals.findings(manifest, rows, at)
    path = home(manifest)/'lead-digest.ndjson'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        items = digest_items(stream)
        if not items and not backlog and not pending and not dead and not opened and not missing and not machine_lines:
            return
        lines = [f"- {i.get('event')}: {i.get('text', '')[:200]}" for i in items[-12:]]
        lines.extend(machine_lines)
        lines += ['- ' + i['text'] for i in missing[:12]]
        lines += [f"- {r['qid']}: unanswered, no default ({r.get('reasks', 0)} re-asks)" for r in unanswered[:12]]
        lines += [f"- {r['qid']}: default deferred: {r['default_deferred']}" for r in deferred]
        if opened:
            oldest = max(opened, key=lambda r: r['age'])
            lines.insert(0, f"- open questions: {len(opened)}; oldest {oldest['qid']} age={int(oldest['age'] // 60)}m; " +
                         '; '.join(f"{r['qid']} {str(r.get('question', ''))[:80]}" for r in opened[:12]))
        if backlog:
            lines.insert(0, f"- backlog: {len(backlog)}; " + ' '.join(r['id'] for r in backlog[:12]))
        if pending:
            lines.insert(0, f"- obligations: {len(pending)} awaiting pickup; " +
                         '; '.join(obligations.line(i) for i in sorted(pending, key=lambda i: (i['priority'] != 'urgent', -i['age']))[:12]))
        # Dead owners: an obligation whose owner is not listening never gets picked up,
        # and its pickup wake lands in that same dead inbox. Name them first, and make the digest an ask
        # (an fyi never wakes the lead) whenever the set of dead-owned items changes.
        dead_ids = sorted(i['id'] for i in dead)
        if dead:
            lines.insert(0, f"- RE-HOME {len(dead)} (owner not listening): " +
                         '; '.join(f"{i['id']}->{i['owner']} {i['age'] / 3600:.1f}h"
                                   for i in sorted(dead, key=lambda i: -i['age'])[:12]) +
                         '. Reassign each to a listening lane, or close it with a reply.')
        kind = 'ask' if dead_ids and dead_ids != state.get('dead_owner_ids') else 'fyi'
        text = (f'coordkit hourly digest: {len(dead)} to re-home, {len(pending)} obligations, '
                f'{len(items) + len(missing) + len(unanswered) + len(deferred)} findings, {len(opened)} open questions; '
                'coordkit obligations report for the full backlog\n' + '\n'.join(lines))
        if manifest.mode == 'live':
            from . import bus
            bus.main(['post', '--from', 'coordkit', '--to', 'lead', '--kind', kind,
                      '--topic', 'coordkit-digest', '--no-mirror', '--force', text], manifest, site_local=True)
        else:
            to_relay_file(manifest, text, at)
        for record in unanswered[:12]:
            questions.mark(manifest, record['qid'], no_default_digested=at)
        for record in deferred:
            questions.mark(manifest, record['qid'], default_deferred_digested=at)
        # A failed publish leaves the queue intact. Keep its inode so concurrent writers cannot
        # append to an unlinked file; there are no sent archives to rotate.
        stream.seek(0); stream.truncate(); stream.flush(); os.fsync(stream.fileno())
    state['lead_digest_at'] = at
    state['dead_owner_ids'] = sorted(i['id'] for i in dead)
    out(f'LEAD_DIGEST posted {len(items)} finding(s), {len(pending)} obligation(s)')


def escalate(manifest, event, text, row=None, deaf=(), fallback='', urgent=False, at=None, probe=None,
             human=False, post=True):
    """Tell someone who is listening. Returns the names/channels reached.

    Order: the declared fallback (resolved to a live name), else the relay; the relay file always.
    Bus rows are written only in live mode (`post`); shadow sweeps reach the relay file.
    """
    reached = []
    # Never back to the row's own recipient: lead-a's fallback is the coordinator role, which resolves to lead-a, so
    # an unacked ask to lead-a used to come straight back to lead-a. It falls to the relay instead.
    own = {(row or {}).get('owner') or (row or {}).get('to')} - {None, ''}
    skip = tuple(deaf) + tuple(own)
    target = resolve_live(manifest, fallback, at, probe, skip) if fallback else None
    if not target and cfg(manifest).relay:
        target = resolve_live(manifest, cfg(manifest).relay, at, probe, skip)
    if target and digest_only(manifest, target):
        # Policy: the lead is not woken by escalations; they batch into one digest an hour.
        if manifest.mode == 'live':
            from .questions import queue_escalation
            queue_escalation(manifest, target, (row or {}).get('topic', 'addressed'), text,
                             key=f"escalation:{event}:{row['id']}" if row else None,
                             default=(row or {}).get('default'),
                             no_auto_default=(row or {}).get('no_auto_default', False), event=event)
        else:
            to_lead_digest(manifest, event, text, row)
        reached.append('lead_digest')
        return reached
    if target and post and manifest.mode == 'live':
        from . import bus
        from . import questions
        copy = {'ts': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'), 'id': secrets.token_hex(3),
                'frm': 'coordkit', 'to': target, 'topic': (row or {}).get('topic', 'addressed'),
                # Always a plain ask: a blocker/ruling copy would be a second open blocker or an
                # unackable ruling to the incumbent bus. The original kind stays in orig_kind.
                'kind': 'ask', 'site_local': True,
                'text': text[:manifest.bus.text_cap], 'escalation': event, 'priority': 'urgent' if urgent else 'normal'}
        if row:
            copy.update(copy_of=row['id'], orig_kind=row.get('kind'), re=row['id'])
            copy['key'] = f"escalation:{event}:{row['id']}"
        duplicate = questions.repeat_bus_ask(manifest, copy, questions.recent_rows(manifest), at)
        if duplicate:
            return [target]
        bus.append([copy], project=False,
                   on_commit=lambda: questions.track_bus_ask(manifest, {k: v for k, v in copy.items() if k != 'copy_of'}))
        reached.append(target)
    if to_relay_file(manifest, text, at):
        reached.append('relay_file')
    return reached


def after_post(manifest, rec, role=None, probe=None):
    """Called by `bus post` for registered names and roles, after the row is appended."""
    if rec.get('frm') == 'coordkit' and rec.get('topic') == 'coordkit-digest':
        return 'delivered'  # the digest is the escalation; re-escalating it would deadlock on the digest lock
    urgent = rec.get('priority') == 'urgent' or priority(rec) == 'urgent'
    actionable = rec['kind'] in WAKE_KINDS or urgent
    if is_human(manifest, role or rec['to']):
        return 'delivered'
    if role and rec['to'] == role:
        if not actionable:
            return 'delivered'
        detail = '; '.join(f'{m}: {listening(manifest, m, probe=probe)[1]}' for m in roles(manifest)[role])
        text = (f"ROLE_VACANT {role}: no live member ({detail}). {rec['kind']} {rec['id']} from {rec['frm']}: "
                f"{rec.get('text', '')[:160]}")
        print(f'role_vacant {role} id={rec["id"]} ({detail})', file=sys.stderr)
        reached = escalate(manifest, 'role_vacant', text, rec, deaf=roles(manifest)[role], urgent=urgent,
                           probe=probe, human=True)
        print(f'ROLE_VACANT {role} id={rec["id"]} escalated={",".join(reached) or "nobody"}')
        return 'role_vacant'
    live, why = listening(manifest, rec['to'], probe=probe)
    if live:
        return 'delivered'
    print(f'recipient_deaf {rec["to"]} id={rec["id"]} ({why})', file=sys.stderr)
    if not actionable:
        print(f'RECIPIENT_DEAF {rec["to"]} id={rec["id"]} ({why}); {rec["kind"]} does not escalate')
        return 'recipient_deaf'
    spec = registry(manifest).get(rec['to'], {})
    text = (f"[deaf: {rec['to']} {why}] {'URGENT ' if urgent else ''}{rec['kind']} {rec['id']} from {rec['frm']}: "
            f"{rec.get('text', '')[:200]}")
    reached = escalate(manifest, 'recipient_deaf', text, rec, deaf=[rec['to']], fallback=spec.get('fallback', ''),
                       urgent=urgent, probe=probe)
    print(f'RECIPIENT_DEAF {rec["to"]} id={rec["id"]} escalated={",".join(reached) or "nobody"}')
    return 'recipient_deaf'


# -- waiting ----------------------------------------------------------------------------------------

def addressed_to(manifest, name, row, at=None, probe=None):
    if row.get('frm') == name:
        return False
    to = row.get('to')
    if to == name or row.get('owner') == name:
        return True
    if to == 'all':
        return row.get('kind') != 'fyi'
    if to in roles(manifest) and name in roles(manifest)[to]:
        return first_live(manifest, to, at, probe) in (None, name)
    return False




class Lease:
    """Persist question-adapter confirmation evidence; read legacy waiter leases."""

    def __init__(self, manifest, name, clock=time.time):
        self.manifest, self.name, self.clock = manifest, name, clock
        self.path = lease_path(manifest, name)


    def renew(self, **fields):
        self.data.update(fields, heartbeat_at=self.clock())
        write_json(self.path, self.data)


# -- acks and inbox ---------------------------------------------------------------------------------

def replies(rows):
    out = {}
    for r in rows:
        if r.get('re'):
            out.setdefault(r['re'], []).append(r)
    return out


def assignees(row, manifest=None):
    """Names whose reply acknowledges this row: its owner or recipient; for a row addressed to a
    role (vacant at send time), any member; for a role-routed row, the member it was routed to."""
    to = row.get('owner') or row.get('to')
    names = {to}
    if manifest is not None and to in roles(manifest):
        names |= set(roles(manifest)[to])
    return names


def ack_state(row, by_re, manifest=None):
    """None, or the latest ack state from an assignee: received | done | blocked | replied.

    A bystander's reply never stops the receipt sweep."""
    state = None
    allowed = assignees(row, manifest)
    for reply in by_re.get(row['id'], []):
        if reply.get('frm') not in allowed or is_escalation(reply):
            continue
        from .handoffs import portable
        if (row.get('kind') == 'handoff' and portable(row)
                and not (reply.get('handoff_verified') == row['id'] or reply.get('handoff_bounce')
                         or reply.get('ack') == 'blocked')):
            continue
        text = (reply.get('ack') or reply.get('text') or '').strip().lower()
        state = next((s for s in ACK_STATES if text.startswith(s)), 'replied')
    return state


def is_escalation(row):
    return bool(row.get('escalation') or row.get('copy_of')) or str(row.get('id', '')).startswith(
        ('wake-', 'alert-', 'reconcile-', 'answer-'))


def open_for(manifest, name, rows, at):
    by_re = replies(rows)
    floor = at - cfg(manifest).window_hours*3600
    return [r for r in rows if r.get('ts') and parse_ts(r['ts']).timestamp() >= floor and not is_escalation(r)
            and r.get('kind') in WAKE_KINDS and r.get('to') != 'all' and addressed_to(manifest, name, r)
            and ack_state(r, by_re, manifest) is None]


def agent_reply(row, name, msg_id):
    """An agent's reply proves pickup; an automatic sidecar receipt never does."""
    return bool(msg_id and row.get('re') == msg_id and row.get('frm') == name
                and not row.get('auto_received') and not is_escalation(row))


def ack(manifest, msg_id, name, state='received', note=''):
    from . import bus
    manifest.writable()
    bus.configure(manifest)
    rows = bus.read()
    orig = next((r for r in rows if r.get('id') == msg_id), None)
    if orig is None:
        raise ManifestError(f'unknown_message: {msg_id}')
    if orig.get('kind') == 'handoff' and state != 'blocked':
        # Use the bus writer lock for portable pickup checks and deduplicated bounces.
        text = f'{state}: {note}' if note else state
        return bus.main(['ack', msg_id, text, '--from', name], manifest)
    if orig.get('kind') == 'ruling' and orig.get('owner') == name and state != 'blocked':
        # Rulings keep their own owner ack (bus ack), which also counts as received/done here.
        return bus.main(['ack', msg_id, note or state, '--from', name], manifest)  # positionals first: bus ack is not intermixed
    if name not in assignees(orig, manifest):
        print(f'warning: {msg_id} is addressed to {orig.get("owner") or orig["to"]}, not {name}; '
              'recorded, but only the assignee\'s ack stops escalation', file=sys.stderr)
    text = f'{state}: {note}' if note else state
    rec = {'ts': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'), 'id': secrets.token_hex(3),
           'frm': name, 'to': orig['frm'], 'topic': orig.get('topic', 'general'), 'kind': 'answer', 're': msg_id,
           'ack': state, 'text': text[:manifest.bus.text_cap]}
    bus.append([rec])
    print(f'acked {msg_id} {state} id={rec["id"]}')
    return 0


def inbox_rows(manifest, name, all_rows=False, at=None):
    """[{id, kind, frm, age_minutes, state, urgent, text}] addressed to name; read-only (no cursor moves)."""
    from . import bus
    bus.configure(manifest)
    rows, at = bus.read(), (time.time() if at is None else at)
    by_re = replies(rows)
    floor = at - cfg(manifest).window_hours*3600
    mine = [r for r in rows if r.get('ts') and parse_ts(r['ts']).timestamp() >= floor and not is_escalation(r)
            and addressed_to(manifest, name, r) and r.get('kind') in WAKE_KINDS + ('msg',)]
    out = []
    for r in mine:
        state = ack_state(r, by_re, manifest)
        if state is None and r.get('to') == 'all' and any(x.get('frm') == name for x in by_re.get(r['id'], [])):
            state = 'done'  # a broadcast has no assignee; the reader's own reply closes it for that reader
        if state in ('done', 'replied') and not all_rows:
            continue
        out.append({'id': r['id'], 'kind': r['kind'], 'frm': r['frm'], 'state': state or 'OPEN',
                    'age_minutes': int((at - parse_ts(r['ts']).timestamp()) // 60),
                    'urgent': priority(r) == 'urgent' or r.get('priority') == 'urgent',
                    'text': ' '.join((r.get('text') or '').split())})
    return out


# -- sweep ------------------------------------------------------------------------------------------

def sweep(manifest, rows=None, at=None, probe=None, dry_run=False, out=print):
    if dry_run:
        return _sweep(manifest, rows, at, probe, True, out)
    with locked(home(manifest)/'sweep.lock'):
        return _sweep(manifest, rows, at, probe, dry_run, out)


def _sweep(manifest, rows=None, at=None, probe=None, dry_run=False, out=print):
    """One pass of the deaf-agent checks. Returns the findings [{key, event, text}] it raised."""
    from . import bus
    bus.configure(manifest)
    rows = bus.read() if rows is None else rows
    at = time.time() if at is None else at
    a = cfg(manifest)
    from . import questions
    if not dry_run:
        questions.reconcile_defaults(manifest, rows, at)
    question_state = questions.load_all(manifest)
    exhausted = {qid for qid, r in question_state.items()
                 if r['state'] == 'closed' or r.get('unanswered_no_default')}
    state_path = home(manifest)/'sweep.json'
    state = read_json(state_path, {}) or {}
    state.pop('deaf_since', None)
    state.pop('baseline', None)
    seen = state.setdefault('seen', {})
    findings = []

    def raise_(key, event, text, once=False, **kw):
        last = seen.get(key)
        if last is not None and (once or at - last < seconds(a.realert_after)):
            return
        findings.append(dict(key=key, event=event, text=text, **kw))

    names = registry(manifest)
    status = {n: listening(manifest, n, at, probe, rows) for n in names}
    for role, members in roles(manifest).items():
        if not is_human(manifest, role) and not any(listening(manifest, m, at, probe, rows)[0] for m in members):
            detail = '; '.join(f'{m}: {listening(manifest, m, at, probe, rows)[1]}' for m in members)
            raise_(f'vacant:{role}', 'role_vacant', f'ROLE_VACANT {role}: no live member ({detail})',
                   deaf=members, human=True)
    # One pickup policy for asks, PR handoffs and auditor flags. Derive backlog afresh so
    # replies and reassignments disappear from the digest without an alert journal.
    pending = [i for i in obligations.report(manifest, rows, at, window_hours=a.window_hours)
               if i['id'] not in exhausted
               if i['frm'] not in (manifest.escalation.internal_senders or [manifest.escalation.internal_to])
               and (i['frm'] not in a.internal_senders or i['priority'] == 'urgent')]
    from .relay import remote_only
    remote = remote_only(manifest, rows)
    def heard(name):
        return (status.get(name) or listening(manifest, name, at, probe, rows))[0]
    dead = [i for i in pending if i['owner'] not in remote and not heard(i['owner'])]
    wake_keys = set()
    for item in pending:
        owner = item['owner']
        token = f"pickup:{item['id']}:{owner}:{int(item['since'])}"
        wake_keys.add(token)
        if owner not in names or digest_only(manifest, owner) or item['age'] < min(seconds(manifest.obligations.ack_after), item['due_after']):
            continue
        # Existing wake rows survive receipt expiry and upgrading from the old reconcile ladder.
        prior = any(r.get('re') == item['id'] and r.get('to') == owner
                    and str(r.get('id', '')).startswith('wake-ob-')
                    and parse_ts(r['ts']).timestamp() >= item['since'] for r in rows)
        legacy = any(k.startswith((f'unacked:{item["id"]}:', f'deaf:{item["id"]}'))
                     and stamp >= item['since'] for k, stamp in seen.items())
        if token in seen or prior or legacy:
            if not dry_run:
                seen.setdefault(token, at)
            continue
        text = 'Pickup: ' + obligations.line(item) + f"; reply --re {item['id']}."
        findings.append(dict(key=token, event='pickup', text=text, item=item))
    for f in findings:
        out(f['text'])
        if dry_run:
            continue
        if f['event'] == 'pickup':
            item = f['item']
            # No idle copy, after_post escalation, or ANX dependency for a direct owner wake.
            if manifest.mode == 'live':
                bus.append([dict(ts=dt.datetime.fromtimestamp(at, dt.timezone.utc).isoformat(),
                id='wake-ob-'+hashlib.sha256(f['key'].encode()).hexdigest()[:20], origin='coordkit',
                frm='coordkit', to=item['owner'], topic='coordkit-pickup', kind='handoff',
                    re=item['id'], text=f['text'])], project=False)
        else:
            to_lead_digest(manifest, f['event'], f['text'], at=at)
        seen[f['key']] = at
    if not dry_run:
        flush_lead_digest(manifest, state, at, out, pending=pending, dead=dead, rows=rows)
        live_keys = wake_keys | {f['key'] for f in findings} | {k for k, v in seen.items() if at - v < (a.window_hours*3600 if k.startswith(('human:', 'unacked:', 'deaf:')) else 2*seconds(a.realert_after))}
        state['seen'] = {k: v for k, v in seen.items() if k in live_keys}
        write_json(state_path, state)
    out(f'SWEEP findings={len(findings)} registered={len(names)} '
        f'listening={sum(1 for v in status.values() if v[0])} roles={len(roles(manifest))}')
    return findings


# -- CLI --------------------------------------------------------------------------------------------

def send(manifest, frm, to, text, kind='ask', urgent=False, re_id=None, topic='general', extra=(), *, origin='coordkit'):
    from . import bus
    manifest.writable()
    argv = ['post', '--from', frm, '--to', to, '--kind', kind, '--topic', topic,
            '--priority', 'urgent' if urgent else 'normal', *extra]
    if re_id:
        argv += ['--re', re_id]
    bus.LAST_POST = None
    bus.main(argv + [text], manifest, origin=origin)
    outcome = (bus.LAST_POST or (None, None))[1]
    if to != 'all' and outcome is None and target_kind(manifest, to) is None:
        print(f'NOT_REGISTERED {to}: no waiter lease can be checked; register it in [addressed.agents]', file=sys.stderr)
    return {'recipient_deaf': EXIT_DEAF, 'role_vacant': EXIT_VACANT}.get(outcome, 0)


def main(argv, manifest, command):
    p = argparse.ArgumentParser(prog=f'coordkit {command}', allow_abbrev=False)
    if command == 'sweep':
        p.add_argument('--dry-run', action='store_true')
        p.add_argument('--json', action='store_true')
        o = p.parse_args(argv)
        found = sweep(manifest, dry_run=o.dry_run, out=(lambda line: None) if o.json else print)
        if manifest.questions.config_dirs and not o.dry_run:
            # Questions ride the same ANX-independent sweep job (the question escalation contract).
            from . import questions
            questions.sweep(manifest, out=(lambda line: None) if o.json else print)
        if manifest.mode == 'live' and not o.dry_run:
            stamp = read_json(manifest.paths.root/'reconcile/state.json', {}).get('last_run', 0)
            if time.time() - stamp >= manifest.reconciler.interval_seconds:
                from . import reconcile
                with contextlib.redirect_stdout(sys.stderr if o.json else sys.stdout):
                    rc = reconcile.main([], manifest)
                if rc not in (0, EXIT_FINDINGS, 4):
                    return rc
        if o.json:
            json.dump([{k: v for k, v in f.items() if k != 'row'} for f in found], sys.stdout)
            print()
        return EXIT_FINDINGS if found else 0
    raise ManifestError(f'not_implemented: {command}')
