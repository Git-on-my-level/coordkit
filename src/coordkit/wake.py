"""Durable live/retired lane policy; absence of a record means live."""
import json
import time
from .manifest import ManifestError
from .state import name_key, read_json, write_json

# Wake resumes only executions that carry this marker plus the lane name. A lane's
# own label also prefixes the workers it launches, so the name alone could resume
# a child session with the parent's inbox.
WAKE_LABEL = 'coordkit.lane.'


def state_path(manifest):
    return manifest.paths.bus_log.parent/'state'/'lanes.json'


def registered(manifest):
    from .addressed import registry
    return set(registry(manifest)) | {a.label for a in manifest.agents} | {a.name for a in manifest.lanes}


def retired(manifest, name):
    return read_json(state_path(manifest), {}).get(name)


def refuse_retired(manifest, name):
    record = retired(manifest, name)
    if record:
        raise ManifestError('lane_retired: ' + name + '; redirect to ' + (record.get('owner') or 'lead'))


def change(manifest, name, owner=None, undo=False):
    from . import bus
    from .addressed import locked
    manifest.writable()
    bus.configure(manifest)
    name = bus.resolve(name)
    if name not in registered(manifest):
        raise ManifestError('lane_not_registered: ' + name)
    if owner:
        owner = bus.resolve(owner)
        if owner == name or owner not in registered(manifest) | {'lead'}:
            raise ManifestError('invalid_redirect_owner: ' + owner)
        refuse_retired(manifest, owner)
    with locked(state_path(manifest).with_suffix('.lock')):
        state = read_json(state_path(manifest), {})
        if undo:
            state.pop(name, None)
        else:
            state[name] = {'owner': owner or 'lead', 'at': bus.now().isoformat()}
        write_json(state_path(manifest), state)
    print(('UNRETIRED ' if undo else 'RETIRED ') + name)
    return 0


def wake_adapters(manifest):
    from .adapters import available
    return [adapter for adapter in available(manifest) if callable(getattr(adapter, 'wake', None))]


def wake_path(manifest, name):
    return manifest.paths.bus_log.parent/'state/wakes'/f'{name_key(name)}.json'


_WARNED_RECORDS = set()


def wake_record(manifest, name):
    """A broken attempt must not prevent the next delivery from waking a lane."""
    path = wake_path(manifest, name)
    missing = object()
    try:
        record = read_json(path, missing)
    except (ValueError, UnicodeError):
        record = None
    if record is missing:
        return {}
    if isinstance(record, dict):
        at, key = record.get('at'), record.get('key')
        if (type(at) in (int, float) and 0 <= at < float('inf')
                and isinstance(key, str) and key
                and isinstance(record.get('status'), str)
                and isinstance(record.get('attempts', []), list)
                and all(isinstance(a, dict) and isinstance(a.get('status'), str)
                        for a in record.get('attempts', []))):
            return record
    if path not in _WARNED_RECORDS:
        import sys
        print(f'WAKE_INVALID_RECORD {path} (ignored)', file=sys.stderr)
        _WARNED_RECORDS.add(path)
    return {}


def recently_woken(manifest, name):
    record = wake_record(manifest, name)
    return (record.get('status') == 'woken' and
            0 <= time.time() - record['at'] < manifest.bus.wake_debounce_minutes * 60)


def on_demand(manifest, name):
    """The harness module for a lane that has no process between turns, else None."""
    from .adapters import hermes
    return hermes if hermes.lane_for(manifest, name) else None


def is_listening(manifest, name, rows):
    from . import addressed
    harness = on_demand(manifest, name)
    if harness is not None:
        # Only a running turn can hear a new row: its runner re-reads the cursor when
        # the turn ends. A recent post or a finished wake proves nothing afterwards.
        lane = harness.lane_for(manifest, name)
        record = wake_record(manifest, name)
        return harness.turn_running(lane) or (
            recently_woken(manifest, name) and not harness.settled(manifest, name, record))
    if recently_woken(manifest, name):
        return True
    if addressed.listening(manifest, name, rows=rows)[0]:
        return True
    # bus wait holds its cursor lock throughout its entire bounded wait.
    import fcntl
    path = manifest.paths.bus_log.parent/'state/cursors'/(name_key(name)+'.wait.lock')
    if path.exists():
        with path.open('a') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
    for adapter in wake_adapters(manifest):
        listening = getattr(adapter, 'listening', None)
        if callable(listening) and listening(manifest, name, rows) is True:
            return True
    return False


def notify(manifest, rows):
    """One durable attempt per debounce window, including failed/unavailable wakes.

    Record intent before contacting the adapter. A crash remains an explicit pending
    attempt and holds the debounce; a post is already durable even if waking fails.
    """
    import time
    import secrets
    from .addressed import locked
    from .state import name_key
    from .bus import aliases, resolve
    book = aliases(manifest)
    names = registered(manifest)
    rows = [dict(row, frm=resolve(row.get('frm', ''), book), to=resolve(row.get('to', ''), book)) for row in rows]
    # Suppress answer/ack feedback, not referenced work. A handoff (or any
    # other work kind) can reference its context and still needs a recipient.
    actionable = [row for row in rows if row.get('kind') != 'fyi' and row.get('to') != 'all'
                  and not row.get('digest_only')
                  and row.get('frm') != row.get('to')
                  and not (row.get('kind') in ('reply', 'answer', 'ack')
                           and (row.get('re') or recently_woken(manifest, row.get('frm', ''))))]
    from .routing import roles as role_members
    members = role_members(manifest)

    def recipients(row):
        """The addressee, plus lanes that only a wake can reach: a waiter or hook shows a
        busy session its role's and its owned rows, but an on-demand lane has neither."""
        extra = [m for m in members.get(row['to'], ()) if on_demand(manifest, m) is not None]
        if row.get('owner') and on_demand(manifest, row['owner']) is not None:
            extra.append(row['owner'])
        return [row['to'], *[m for m in extra if m != row.get('frm')]]

    # An owned row can be addressed to anyone, `all` included; its on-demand owner still needs a turn.
    owners = [row['owner'] for row in rows if row.get('owner') and row.get('kind') != 'fyi'
              and not row.get('digest_only') and row.get('frm') != row['owner']
              and not (row.get('kind') in ('reply', 'answer', 'ack') and row.get('re'))
              and on_demand(manifest, row['owner']) is not None]
    for name in dict.fromkeys([*(name for row in actionable for name in recipients(row)), *owners]):
        if name in names and not retired(manifest, name) and on_demand(manifest, name) is not None:
            # Ask before looking: a runner about to release its lock still finds the request.
            on_demand(manifest, name).request_turn(on_demand(manifest, name).lane_for(manifest, name))
        if name not in names or retired(manifest, name) or is_listening(manifest, name, rows):
            continue
        path = wake_path(manifest, name)
        with locked(path.with_suffix('.lock')):
            at = time.time()
            record = wake_record(manifest, name)
            harness = on_demand(manifest, name)
            if record and at - record['at'] < manifest.bus.wake_debounce_minutes * 60 and not (
                    harness is not None and harness.settled(manifest, name, record)):
                continue
            # A pending attempt may already have launched its continuation. Replay
            # its idempotency key after a crash, even beyond the debounce window.
            pending = record.get('status') == 'pending'
            key = record['key'] if pending else manifest.agentctl.request_key_prefix + '-wake-' + name + '-' + secrets.token_hex(8)
            attempts = record.get('attempts', []) if pending else []
            record = dict(at=at, lane=name, key=key, status='pending', ids=[r.get('id') for r in rows if r.get('to') == name], attempts=attempts)
            write_json(path, record)
            # An on-demand lane belongs to its harness alone; other lanes never try it.
            owner = 'hermes' if harness is not None else None
            for adapter in wake_adapters(manifest):
                if (adapter.name == 'hermes') != (owner == 'hermes'):
                    continue
                attempt = {'adapter': adapter.name, 'status': 'pending'}
                record['attempts'].append(attempt)
                write_json(path, record)
                try:
                    attempt.update(adapter.wake(manifest, name, rows, key))
                except Exception as exc:
                    attempt.update(status='failed', reason=str(exc))
                write_json(path, record)
                if attempt['status'] in ('woken', 'listening'):
                    break
            record['status'] = (record['attempts'][-1]['status'] if record['attempts'] else 'unavailable')
            record['finished_at'] = time.time()
            write_json(path, record)
            journal = path.parent/'attempts.jsonl'
            with journal.open('a') as stream:
                import fcntl, os
                fcntl.flock(stream, fcntl.LOCK_EX)
                stream.write(json.dumps(record) + '\n')
                stream.flush(); os.fsync(stream.fileno())


def scan(manifest, dry_run=False):
    """Wake for rows appended by writers that bypass bus.append (e.g. an older bus CLI).

    Keeps a byte checkpoint beside the wake records. The first run, or a replaced or
    truncated log, starts at the current end, so no backlog is ever woken.
    """
    from .addressed import locked
    from .tail import TailReader
    path = manifest.paths.bus_log.parent/'state/wakes/scan.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    if not dry_run:
        # Due timers become ordinary rows first, so this same scan can wake their lanes.
        try:
            from .timers import fire
            fired = fire(manifest)
            if fired:
                print('WAKE_TIMERS fired=' + ','.join(fired))
        except Exception as exc:
            import sys
            print(f'WAKE_TIMERS failed: {type(exc).__name__}: {exc}', file=sys.stderr)
    with locked(path.with_suffix('.lock')):
        reader = TailReader(manifest.paths.bus_log)
        mark = read_json(path, {})
        # A first/replaced/truncated log starts at its end. Subsequent scans use
        # the same complete-line byte reader as relay and local bus readers.
        stat = manifest.paths.bus_log.stat()
        valid = (mark.get('file') == [stat.st_dev, stat.st_ino]
                 and type(mark.get('offset')) is int
                 and 0 <= mark['offset'] <= stat.st_size)
        reader.state = mark if valid else None
        _, lines = reader.read(mark.get('count', 0) if valid else 10 ** 18)
        rows = []
        for _, line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            # Relayed rows are scanned too: a crash between the relay append and its
            # wake would otherwise lose the wake. The debounce absorbs the repeat.
            if isinstance(row, dict):
                for to in str(row.get('to', '')).split(','):
                    rows.append(dict(row, to=to.strip()))
        if dry_run:
            print(f'WAKE_SCAN rows={len(rows)} (dry run)')
            return 0
        if rows:
            notify(manifest, rows)
        write_json(path, reader.state)
    print(f'WAKE_SCAN rows={len(rows)}')
    return 0
