"""Self-hosted relay sink, durable outbox, authenticated applier and service CLI."""
import fcntl
import json
import os
import random
import re
import signal
import socket
import sqlite3
import stat
import threading
import time
from .manifest import ManifestError
from .state import read_json, write_json
from .tail import TailReader
from .websocket import MAX_MESSAGE, ProtocolError, WebSocket, endpoint

FRAME_TIMEOUT = 75

SITE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')


def validate(config):
    endpoint(config.url)
    if not SITE.fullmatch(config.site) or not config.secret_file:
        raise ManifestError('invalid_value: relay.site / relay.secret_file')
    if not 1 <= config.heartbeat_seconds < FRAME_TIMEOUT:
        raise ManifestError('invalid_value: relay.heartbeat_seconds must be 1..74')


def secret(manifest):
    from pathlib import Path
    path = manifest.file.parent / Path(manifest.relay.secret_file).expanduser()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
            raise ManifestError('relay_secret_requires_owned_0600_file')
        token = stream.read(4097).strip()
    if not 32 <= len(token) <= 4096 or any(c < 33 or c > 126 for c in token):
        raise ManifestError('relay_secret_invalid')
    return token.decode('ascii')


def directory(manifest):
    return manifest.paths.root/'relay'


class Store:
    def __init__(self, manifest):
        self.manifest = manifest
        from .bus import ReplayIndex
        self.replay_index = ReplayIndex(manifest.paths.bus_log)
        path = directory(manifest)
        path.mkdir(parents=True, exist_ok=True)
        dbpath = path/'state.sqlite3'
        fd = os.open(dbpath, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ManifestError('relay_outbox_requires_owned_regular_file')
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        self.db = sqlite3.connect(dbpath, timeout=30)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS outbox (seq INTEGER PRIMARY KEY, row TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0)')
        self.db.execute('CREATE TABLE IF NOT EXISTS skipped (seq INTEGER PRIMARY KEY, id TEXT)')
        identity = [manifest.relay.url, manifest.relay.site, str(manifest.paths.bus_log)]
        previous = self.get('identity')
        if previous is not None and previous != identity:
            self.close()
            raise ManifestError('relay_identity_changed: restore config or use a fresh bus/state and site')
        self.put('identity', identity)
        self.db.commit()

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value)))

    def enqueue(self, rows):
        with self.db:
            if not self.db.in_transaction:
                self.db.execute('BEGIN IMMEDIATE')
            sent_through = self.get('sent_through', -1)
            for row in rows:
                if str(row.get('origin', '')).startswith('relay:'):
                    continue
                seq = row.get('seq')
                if type(seq) is not int or seq < 0:
                    raise ProtocolError('relay_invalid_local_seq')
                if seq <= sent_through:
                    continue  # a delayed sink must not resurrect a pruned row
                frame = dict(type='row', site=self.manifest.relay.site, seq=seq, row=row)
                if len(json.dumps(frame, ensure_ascii=True).encode()) > MAX_MESSAGE - 64:
                    self.db.execute('INSERT OR IGNORE INTO skipped VALUES (?,?)', (seq, row.get('id')))
                    self.db.execute('DELETE FROM outbox WHERE seq=?', (seq,))
                    continue
                self.db.execute('INSERT OR IGNORE INTO outbox(seq,row) VALUES (?,?)', (seq, json.dumps(row)))

    def scan(self):
        log = self.manifest.paths.bus_log
        if not log.exists():
            if self.get('scan'):
                raise ProtocolError('relay_log_replaced: restore missing log')
            return
        reader = TailReader(log)
        mark = self.get('scan', {})
        info = log.stat()
        if mark and (mark['file'] != [info.st_dev, info.st_ino] or info.st_size < mark['offset']):
            raise ProtocolError('relay_log_replaced: restore log or use fresh bus/state and site')
        reader.state = mark
        if not mark:
            # First attachment publishes only future rows, like wake-scan and delivery
            # hooks: an existing history would burn relay requests and exceed retention.
            reader.read(10 ** 18)
            with self.db:
                self.put('scan', reader.state)
            return
        _, lines = reader.read(mark.get('count', 0))
        if reader.reset:
            raise ProtocolError('relay_log_replaced: restore log or use fresh bus/state and site')
        from .bus import normalize
        rows = []
        for index, line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                # Physical line index is the source sequence, including v1 writers.
                rows.append(dict(normalize(index, row), seq=index))
        self.enqueue(rows)  # commit before checkpoint; a crash safely repeats
        with self.db:
            self.put('scan', reader.state)
            old_sent = self.db.execute('SELECT max(seq) FROM outbox WHERE sent=1').fetchone()[0]
            self.put('sent_through', max(self.get('sent_through', -1), old_sent if old_sent is not None else -1))
            self.db.execute('DELETE FROM outbox WHERE sent=1 AND seq<?', (reader.state['count'],))

    def pending(self):
        return [dict(type='row', site=self.manifest.relay.site, seq=seq, row=json.loads(row))
                for seq, row in self.db.execute(
                    'SELECT seq,row FROM outbox WHERE sent=0 AND seq<? ORDER BY seq LIMIT 32',
                    (self.get('scan', {}).get('count', 0),))]

    def handle(self, frame, *, sent=None):
        if not isinstance(frame, dict):
            raise ProtocolError('relay_invalid_message')
        kind = frame.get('type')
        if kind == 'gap':
            raise ProtocolError('relay_retention_gap: restore missing rows before resuming')
        if kind == 'accepted':
            if frame.get('site') != self.manifest.relay.site or type(frame.get('seq')) is not int:
                raise ProtocolError('relay_invalid_accept')
            if type(frame.get('duplicate', False)) is not bool:
                raise ProtocolError('relay_invalid_accept')
            if sent is None or frame['seq'] not in sent:
                raise ProtocolError('relay_unsolicited_accept')
            with self.db:
                self.db.execute('UPDATE outbox SET sent=1 WHERE seq=?', (frame['seq'],))
                self.put('sent_through', max(frame['seq'], self.get('sent_through', -1)))
                if frame.get('duplicate', False):
                    self.put('suppressed_rows', self.get('suppressed_rows', 0) + 1)
            sent.remove(frame['seq'])
            return
        if kind == 'ready':
            head = frame.get('head')
            if type(head) is not int or head < self.get('after', 0):
                raise ProtocolError('relay_server_rewound')
            with self.db:
                self.put('head', head)
            return
        if kind != 'row':
            raise ProtocolError('relay_unexpected_message')
        site, seq, row, rseq = (frame.get(k) for k in ('site', 'seq', 'row', 'relay_seq'))
        if (not isinstance(site, str) or not SITE.fullmatch(site) or type(seq) is not int or seq < 0
                or type(rseq) is not int or rseq < 1 or not isinstance(row, dict)
                or not isinstance(row.get('id'), str) or not row['id']
                or any(not isinstance(row.get(k), str) for k in ('ts', 'frm', 'to', 'kind'))
                or str(row.get('origin', '')).startswith('relay:') or '_relay' in row):
            raise ProtocolError('relay_invalid_row')
        after = self.get('after', 0)
        if rseq <= after:
            return
        if rseq != after + 1:
            raise ProtocolError('relay_sequence_gap')
        if site != self.manifest.relay.site:
            from . import bus
            bus.configure(self.manifest)
            bus.append([dict(row, origin='relay:' + site, _relay=dict(seq=seq, relay_seq=rseq))], project=False, replay_index=self.replay_index)
        with self.db:
            self.put('after', rseq)
            self.put('head', max(rseq, self.get('head', 0)))


def notify(manifest, rows):
    if manifest.relay is None or manifest.mode == 'shadow':
        return
    store = Store(manifest)
    try:
        store.enqueue(rows)
    finally:
        store.close()


def status(manifest):
    if manifest.relay is None:
        return dict(enabled=False)
    value = read_json(directory(manifest)/'status.json', {})
    age = time.time() - value.get('at', 0)
    value['connected'] = bool(value.get('connected') and 0 <= age < 90)
    last = value.pop('last_frame', None)
    value['last_frame_age'] = round(max(0, time.time() - last), 1) if last else None
    return dict(enabled=True, **value)


def health(manifest):
    value = status(manifest)
    if not value['enabled']:
        return 'off'
    detail = json.dumps(value, sort_keys=True)
    if not value['connected'] or value.get('last_frame_age') is None or value['last_frame_age'] >= 90 or value.get('error'):
        raise ManifestError('relay_unhealthy ' + detail)
    if value.get('suppressed_rows', 0):
        return 'WARN suppressed_rows: possible reused site name or acceptance retry; ' + detail
    return detail


class StatusWriter:
    """Persist changes in state, or refresh timestamps at most every ten seconds."""
    def __init__(self, path):
        self.path = path
        self.previous = None
        self.last_written = None

    def record(self, value, *, force=False):
        state = {k: v for k, v in value.items() if k not in ('at', 'last_frame')}
        at = time.monotonic()
        if (force or state != self.previous or self.last_written is None
                or at - self.last_written >= 10):
            write_json(self.path, dict(value, at=time.time()))
            self.previous = state
            self.last_written = at


def run(manifest, stop=None):
    manifest.writable()
    secret(manifest)  # fail before retrying configuration/security errors
    path = directory(manifest)
    path.mkdir(parents=True, exist_ok=True)
    lock = open(path/'run.lock', 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise ManifestError('relay_already_running') from None
    stop = stop or threading.Event()
    handlers = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            handlers[sig] = signal.signal(sig, lambda *_: stop.set())
    try:
        store = Store(manifest)
    except BaseException:
        lock.close()
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        raise
    ws = None
    last_frame = None
    error = None
    delay = 1
    writer = StatusWriter(path/'status.json')
    def record(connected):
        writer.record(dict(connected=connected, last_frame=last_frame,
                      lag_rows=max(0, store.get('head', 0) - store.get('after', 0)),
                      pending_rows=store.db.execute('SELECT count(*) FROM outbox WHERE sent=0').fetchone()[0],
                      skipped_rows=[dict(seq=seq, id=rid) for seq, rid in store.db.execute('SELECT seq,id FROM skipped ORDER BY seq')],
                      suppressed_rows=store.get('suppressed_rows', 0), error=error))
    try:
        while not stop.is_set():
            try:
                store.scan()
                ws = WebSocket.connect(manifest.relay.url, secret(manifest))
                ws.send(dict(type='hello', version=1, site=manifest.relay.site, after=store.get('after', 0)))
                sent = set()
                last_frame = time.time()
                ping_at = 0
                error = None
                while not stop.is_set():
                    store.scan()
                    for frame in store.pending():
                        if frame['seq'] not in sent:
                            ws.send(frame)
                            sent.add(frame['seq'])
                    if time.time() - ping_at >= manifest.relay.heartbeat_seconds:
                        ws.send(dict(type='ping'))
                        ping_at = time.time()
                    record(True)
                    try:
                        frame = ws.receive()
                    except socket.timeout:
                        if time.time() - last_frame > FRAME_TIMEOUT:
                            raise ProtocolError('relay_frame_timeout')
                        continue
                    if frame != {'type': 'pong'}:
                        store.handle(frame, sent=sent)
                        if frame.get('type') == 'row':
                            ws.send(dict(type='ack', relay_seq=store.get('after', 0)))
                    last_frame = time.time()
                    delay = 1
            except (OSError, EOFError, ValueError, ManifestError) as exc:
                # Never log remote payloads, bearer tokens, or URL contents.
                known = str(exc).split(':', 1)[0]
                error = known if re.fullmatch(r'relay_[a-z_]+', known) else type(exc).__name__
                record(False)
                if error in ('relay_auth_failed', 'relay_retention_gap', 'relay_server_rewound', 'relay_log_replaced'):
                    return 1
            finally:
                if ws:
                    ws.close(graceful=stop.is_set())
                    ws = None
            stop.wait(delay + random.random())
            delay = min(60, delay * 2)
        return 0
    finally:
        record(False)
        store.close()
        lock.close()
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def spec(manifest):
    from .paths import python_env
    return dict(Label=manifest.launchd.label_prefix+'.relay',
                ProgramArguments=[manifest.launchd.python, '-m', 'coordkit.cli', '--manifest', str(manifest.file), 'relay', 'run'],
                WorkingDirectory=str(manifest.file.parent),
                EnvironmentVariables={'PATH': ':'.join(manifest.launchd.path) or os.defpath, **python_env()},
                RunAtLoad=True, KeepAlive={'SuccessfulExit': False},
                StandardOutPath=str(directory(manifest)/'stdout.log'),
                StandardErrorPath=str(directory(manifest)/'stderr.log'))


def main(argv, manifest):
    from .ops import parser_for
    opts = parser_for('relay').parse_args(argv)
    if opts.action == 'status':
        print(json.dumps(status(manifest), sort_keys=True))
        return 0
    if manifest.relay is None:
        raise ManifestError('relay_unconfigured')
    if opts.action == 'run':
        return run(manifest)
    from .services import manager
    service = manager(manifest)
    job = spec(manifest)
    if opts.action == 'uninstall':
        manifest.writable()
        service.uninstall(job['Label'])
        (directory(manifest)/'service.json').unlink(missing_ok=True)
        return 0
    if not opts.apply:
        rendered = service.render(job)
        print(rendered.decode() if isinstance(rendered, bytes) else json.dumps(rendered, indent=2))
        return 0
    manifest.writable()
    secret(manifest)
    directory(manifest).mkdir(parents=True, exist_ok=True)
    receipt = directory(manifest)/'service.json'
    previous = receipt.exists()
    write_json(receipt, job)
    service.install(job, previous=previous)
    from .selfupdate import register
    register(manifest)
    print('RELAY_INSTALLED')
    return 0
