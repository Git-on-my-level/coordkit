"""Offline protocol and cross-process two-site acceptance tests; no cloud access."""
import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from coordkit import bus, relay, wake
from coordkit.manifest import load, ManifestError
from coordkit.websocket import WebSocket, ProtocolError

TOKEN = 'test-only-' + 'x' * 40


def row(identity='one', **extra):
    return dict(ts='2026-01-01T00:00:00+00:00', id=identity, frm='lead', to='worker', kind='ask', text='hello', **extra)


class FakeRelay(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        super().__init__(('127.0.0.1', 0), Handler)
        self.lock = threading.RLock()
        self.log = []
        self.seen = {}
        self.clients = {}
        self.auth_failures = 0
        self.retain = 256
        self.duplicate = False
        self.drop_accept_for = set()
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return 'ws://127.0.0.1:' + str(self.server_address[1]) + '/bus/test'

    def disconnect(self, site):
        with self.lock:
            ws = self.clients.pop(site, None)
            if ws:
                with contextlib.suppress(OSError):
                    ws.sock.shutdown(socket.SHUT_RDWR)
                ws.close()

    def close(self):
        for site in list(self.clients):
            self.disconnect(site)
        self.shutdown()
        self.server_close()
        self.thread.join(3)


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        server = self.server
        site = None
        ws = None
        try:
            data = b''
            while b'\r\n\r\n' not in data:
                data += self.request.recv(1)
                if len(data) > 16384:
                    return
            headers = dict(line.split(': ', 1) for line in data.decode().split('\r\n')[1:] if ': ' in line)
            if headers.get('Authorization') != 'Bearer ' + TOKEN:
                server.auth_failures += 1
                self.request.sendall(b'HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n')
                return
            accept = base64.b64encode(hashlib.sha1((headers['Sec-WebSocket-Key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
            self.request.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
            ws = WebSocket(self.request, client=False)
            hello = ws.receive()
            if hello.get('type') != 'hello' or hello.get('version') != 1:
                return
            site = hello['site']
            with server.lock:
                after = hello['after']
                oldest = max(1, len(server.log) - server.retain + 1)
                if after < oldest - 1 or after > len(server.log):
                    ws.send(dict(type='gap', oldest=oldest, head=len(server.log)))
                    return
                server.clients[site] = ws
                ws.send(dict(type='ready', head=len(server.log)))
                for frame in server.log[after:]:
                    ws.send(frame)
            while True:
                frame = ws.receive()
                with server.lock:
                    if frame['type'] == 'ping':
                        ws.send(dict(type='pong'))
                    elif frame['type'] == 'row':
                        if not frame['site'] == site:
                            return
                        duplicate = frame['seq'] <= server.seen.get(site, -1)
                        if not duplicate:
                            server.seen[site] = frame['seq']
                            frame = dict(frame, relay_seq=len(server.log) + 1)
                            server.log.append(frame)
                            for peer in list(server.clients.values()):
                                with contextlib.suppress(OSError):
                                    peer.send(frame)
                                    if server.duplicate:
                                        peer.send(frame)
                        if site in server.drop_accept_for:
                            server.drop_accept_for.remove(site)
                            self.request.shutdown(socket.SHUT_RDWR)
                            return
                        ws.send(dict(type='accepted', site=site, seq=frame['seq'], duplicate=duplicate))
                    elif frame['type'] != 'ack':
                        return
        except (EOFError, OSError, ValueError):
            pass
        finally:
            with server.lock:
                if server.clients.get(site) is ws:
                    server.clients.pop(site, None)


class RelayCase(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).parents[1]/'.coordkit'
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def site(self, name='a', url='ws://127.0.0.1:1/bus/test'):
        root = self.root/name
        root.mkdir()
        secret = root/'secret'
        secret.write_text(TOKEN + '\n')
        secret.chmod(0o600)
        config = root/'coordkit.toml'
        config.write_text('[project]\nname="sample"\ngit_repo="owner/repo"\n[bus]\nmirror=false\n'
                          '[relay]\nurl=' + json.dumps(url) + '\nsite=' + json.dumps(name) + '\nsecret_file="secret"\n')
        return load(config)

    def post(self, manifest, identity, **extra):
        bus.configure(manifest)
        bus.append([row(identity, **extra)])

    def frame(self, rseq=1, seq=0, identity='one'):
        return dict(type='row', site='remote', seq=seq, relay_seq=rseq, row=row(identity, text_hash='test-hash'))

    def test_sink_scan_legacy_partial_and_loop_prevention(self):
        m = self.site()
        log = m.paths.bus_log
        log.parent.mkdir(parents=True, exist_ok=True)
        log.touch()
        store = relay.Store(m)
        self.addCleanup(store.close)
        store.scan()  # attach before any rows
        self.post(m, 'local')
        with log.open('a') as f:
            f.write(json.dumps(row('legacy')) + '\nnot json\n{"id":')
        store.scan()
        self.assertEqual([(f['seq'], f['row']['id']) for f in store.pending()], [(0, 'local'), (1, 'legacy')])
        # Finish the pending legacy write, then apply a remote row through append.
        with log.open('a') as f:
            f.write('"partial", "ts":"t", "frm":"lead", "to":"worker", "kind":"ask"}\n')
        store.scan()
        with patch('coordkit.wake.notify') as notify:
            store.handle(self.frame())
        notify.assert_called_once()
        store.scan()
        self.assertEqual([f['row']['id'] for f in store.pending()], ['local', 'legacy', 'partial'])
        self.assertEqual(bus.read()[-1]['origin'], 'relay:remote')

    def test_first_attachment_publishes_only_future_rows(self):
        m = self.site()
        log = m.paths.bus_log
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(''.join(json.dumps(row(f'old{i}')) + '\n' for i in range(3)))
        store = relay.Store(m)
        self.addCleanup(store.close)
        store.scan()
        self.assertEqual(store.pending(), [])
        with log.open('a') as f:
            f.write(json.dumps(row('new')) + '\n')
        store.scan()
        self.assertEqual([(f['seq'], f['row']['id']) for f in store.pending()], [(3, 'new')])

    def test_publish_only_scanned_prefix_even_when_sinks_arrive_out_of_order(self):
        m = self.site()
        self.post(m, 'first')
        store = relay.Store(m)
        self.addCleanup(store.close)
        self.assertEqual(store.pending(), [])
        store.scan()
        self.assertEqual([f['seq'] for f in store.pending()], [0])
        # An older writer has no sink; a later writer's sink can race the scanner.
        with m.paths.bus_log.open('a') as f:
            f.write(json.dumps(row('older-writer')) + '\n')
        self.post(m, 'later-sink')
        self.assertEqual([f['seq'] for f in store.pending()], [0])
        store.scan()
        self.assertEqual([f['seq'] for f in store.pending()], [0, 1, 2])
        m.paths.bus_log.unlink()
        with self.assertRaisesRegex(ProtocolError, 'log_replaced'):
            store.scan()

    def test_replay_dedup_source_seq_crash_boundary_and_no_projection(self):
        m = self.site()
        store = relay.Store(m)
        self.addCleanup(store.close)
        with patch('coordkit.wake.notify') as notify, patch('coordkit.bus.project_pending') as project:
            store.handle(self.frame())
            store.handle(self.frame())
            # Simulate checkpoint loss after append/fsync.
            with store.db:
                store.put('after', 0)
            store.handle(self.frame())
            # A colliding short row ID at a new source sequence is distinct work.
            store.handle(self.frame(2, 3))
            self.assertEqual(len(bus.read()), 2)
            self.assertEqual(sum(bool(c.args[1]) for c in notify.call_args_list), 2)
            project.assert_not_called()
        self.assertEqual(store.get('after'), 2)
        m.anx.workspace = 'test'
        m.bus.mirror = True
        bus.configure(m)
        with patch('coordkit.adapters.builtin.Anx.project') as project:
            bus.project_pending()
        project.assert_not_called()

    def test_foreign_append_wakes_local_adapter_and_hook_delivers_on_each_host(self):
        from coordkit import deliver
        from coordkit.state import write_json
        for name in ('hook-a', 'hook-b'):
            m = self.site(name)
            m.addressed.agents = {'worker': {}}
            m.paths.bus_log.parent.mkdir(parents=True, exist_ok=True)
            m.paths.bus_log.touch()
            info = m.paths.bus_log.stat()
            write_json(deliver.cursor_path(m, 'worker'), {'file': [info.st_dev, info.st_ino], 'offset': 0})
            adapter = Mock(spec=['name', 'wake', 'listening'])
            adapter.name = 'test'
            adapter.listening.return_value = False
            adapter.wake.return_value = {'status': 'woken'}
            store = relay.Store(m)
            try:
                with patch('coordkit.wake.wake_adapters', return_value=[adapter]), patch('coordkit.addressed.listening', return_value=(False, 'idle')):
                    store.handle(self.frame())
                adapter.wake.assert_called_once()
                out = io.StringIO()
                with patch('coordkit.identity.asker_for', return_value=('worker', 'test')):
                    deliver.deliver(m, {'hook_event_name': 'PostToolUse'}, out)
                self.assertIn('BUS ask one', out.getvalue())
                self.assertEqual(store.pending(), [])
            finally:
                store.close()

    def test_gap_duplicate_and_retention_fail_closed(self):
        m = self.site()
        store = relay.Store(m)
        self.addCleanup(store.close)
        with self.assertRaisesRegex(ProtocolError, 'sequence_gap'):
            store.handle(self.frame(2))
        self.assertFalse(m.paths.bus_log.exists())
        self.assertEqual(store.get('after', 0), 0)
        with self.assertRaisesRegex(ProtocolError, 'retention_gap'):
            store.handle(dict(type='gap', oldest=9, head=10))
        with self.assertRaisesRegex(ProtocolError, 'rewound'):
            store.put('after', 10)
            store.handle(dict(type='ready', head=0))

    def test_manifest_off_invalid_urls_secret_file_and_identity(self):
        m = self.site()
        self.assertEqual(relay.secret(m), TOKEN)
        secret = m.path(m.relay.secret_file)
        secret.chmod(0o644)
        with self.assertRaisesRegex(ManifestError, '0600'):
            relay.secret(m)
        secret.chmod(0o600)
        secret.rename(secret.with_suffix('.real'))
        secret.symlink_to(secret.with_suffix('.real'))
        with self.assertRaises(OSError):
            relay.secret(m)
        for url in ('ws://example.test/bus/a', 'wss://a/\r\nx', 'wss://a/?secret=x', 'https://a', 'wss://user:pass@a'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                relay.endpoint(url)
        store = relay.Store(m)
        store.close()
        m.relay.site = 'other'
        with self.assertRaisesRegex(ManifestError, 'identity_changed'):
            relay.Store(m)
        m.relay = None
        self.assertEqual(relay.status(m), {'enabled': False})
        self.assertEqual(relay.health(m), 'off')

    def test_log_replacement_stops_and_status_goes_stale(self):
        m = self.site()
        self.post(m, 'one')
        store = relay.Store(m)
        self.addCleanup(store.close)
        store.scan()
        m.paths.bus_log.write_text('')
        with self.assertRaisesRegex(ProtocolError, 'log_replaced'):
            store.scan()
        from coordkit.state import write_json
        write_json(relay.directory(m)/'status.json', {'at': time.time() - 100, 'connected': True, 'last_frame': time.time() - 100})
        self.assertFalse(relay.status(m)['connected'])
        with self.assertRaisesRegex(ManifestError, 'unhealthy'):
            relay.health(m)

    def test_install_renders_both_managers_without_mutation_and_shadow_guard(self):
        from coordkit.services import Launchd, Systemd
        m = self.site()
        job = relay.spec(m)
        self.assertNotIn(TOKEN, json.dumps(job))
        self.assertIn(b'relay', Launchd(m).render(job))
        self.assertIn('Restart=on-failure', next(iter(Systemd(m).render(job).values())))
        with patch('coordkit.relay.secret') as secret, patch('coordkit.services.checked') as checked, contextlib.redirect_stdout(io.StringIO()):
            relay.main(['install'], m)
        checked.assert_not_called()
        secret.assert_not_called()
        m.mode = 'shadow'
        with self.assertRaisesRegex(ManifestError, 'shadow_mode'):
            relay.main(['run'], m)

    def test_heartbeat_config_default_range_and_runner_interval(self):
        m = self.site()
        self.assertEqual(m.relay.heartbeat_seconds, 50)
        original = m.file.read_text()
        for value in ('0', '75', '-1', 'true', '1.5', '"50"'):
            m.file.write_text(original + '\nheartbeat_seconds=' + value + '\n')
            with self.subTest(value=value), self.assertRaises(ManifestError):
                load(m.file)
        m.file.write_text(original + '\nheartbeat_seconds=3\n')
        m = load(m.file)
        stop = threading.Event()
        ws = Mock()
        now = [100.0]
        def receive():
            now[0] += 3
            if now[0] >= 106:
                stop.set()
            return {'type': 'pong'}
        ws.receive.side_effect = receive
        with patch.object(relay.WebSocket, 'connect', return_value=ws), patch('coordkit.relay.time.time', side_effect=lambda: now[0]):
            self.assertEqual(relay.run(m, stop), 0)
        self.assertEqual(sum(c.args[0] == {'type': 'ping'} for c in ws.send.call_args_list), 2)
        ws.close.assert_called_once_with(graceful=True)

    def test_accept_requires_sent_on_this_connection_and_prunes_only_sent_rows(self):
        m = self.site()
        self.post(m, 'one')
        self.post(m, 'two')
        store = relay.Store(m)
        try:
            store.scan()
            accepted = dict(type='accepted', site=m.relay.site, seq=0, duplicate=True)
            for sent in (None, set(), {1}):
                with self.assertRaisesRegex(ProtocolError, 'unsolicited_accept'):
                    store.handle(accepted, sent=sent)
            sent = {0}
            store.handle(accepted, sent=sent)
            self.assertEqual(sent, set())
            self.assertEqual(store.get('suppressed_rows'), 1)
            self.assertEqual(store.db.execute('SELECT seq,sent FROM outbox ORDER BY seq').fetchall(), [(0, 1), (1, 0)])
            store.scan()
            self.assertEqual(store.db.execute('SELECT seq FROM outbox').fetchall(), [(1,)])
            # A delayed sink cannot recreate a pruned row; an unsent row stays.
            store.enqueue([dict(row('one'), seq=0)])
            self.assertEqual([f['seq'] for f in store.pending()], [1])
        finally:
            store.close()
        dbpath = relay.directory(m)/'state.sqlite3'
        dbpath.chmod(0o666)
        store = relay.Store(m)
        try:
            self.assertEqual(dbpath.stat().st_mode & 0o777, 0o600)
            self.assertEqual(store.get('suppressed_rows'), 1)
        finally:
            store.close()

    def test_oversized_legacy_row_is_skipped_without_losing_valid_batch_rows(self):
        server = FakeRelay()
        self.addCleanup(server.close)
        m = self.site(url=server.url)
        log = m.paths.bus_log
        log.parent.mkdir(parents=True, exist_ok=True)
        log.touch()
        attach = relay.Store(m)
        attach.scan()
        attach.close()
        records = [row('before'), dict(row('too-large'), text='x' * 70000), row('after')]
        with log.open('a') as f:
            f.write(''.join(json.dumps(r) + '\n' for r in records))
        proc = self.start(m)
        self.until(lambda: len(server.log) == 2 and relay.status(m).get('pending_rows') == 0)
        self.assertEqual([f['row']['id'] for f in server.log], ['before', 'after'])
        self.assertEqual(relay.status(m)['skipped_rows'], [{'seq': 1, 'id': 'too-large'}])
        self.assertIsNone(proc.poll())
        self.stop(proc)
        store = relay.Store(m)
        try:
            self.assertEqual(store.get('scan')['count'], 3)
            store.scan()
            self.assertEqual(store.pending(), [])
            self.assertEqual(store.db.execute('SELECT count(*) FROM outbox').fetchone()[0], 0)
        finally:
            store.close()

    def test_status_writes_on_state_change_or_ten_seconds_not_every_frame(self):
        writer = relay.StatusWriter(self.root/'status.json')
        base = dict(connected=True, pending_rows=0, last_frame=100)
        with patch('coordkit.relay.write_json') as write, patch('coordkit.relay.time.monotonic', side_effect=[0, 1, 9, 10, 10.1]):
            writer.record(base)
            writer.record(dict(base, last_frame=101))
            writer.record(dict(base, last_frame=109))
            self.assertEqual(write.call_count, 1)
            writer.record(dict(base, last_frame=110))
            self.assertEqual(write.call_count, 2)
            writer.record(dict(base, connected=False, last_frame=110))
            self.assertEqual(write.call_count, 3)

    def test_doctor_health_warns_for_suppressed_rows(self):
        from coordkit.state import write_json
        m = self.site()
        write_json(relay.directory(m)/'status.json', dict(connected=True, at=time.time(), last_frame=time.time(), suppressed_rows=1))
        self.assertTrue(relay.health(m).startswith('WARN suppressed_rows'))

    def test_peer_protocol_errors_reconnect_through_bounded_backoff(self):
        for reason in ('relay_invalid_upgrade', 'relay_invalid_json'):
            m = self.site(reason)
            stop = threading.Event()
            ws = Mock()
            ws.receive.side_effect = ProtocolError(reason)
            wait = Mock(side_effect=lambda *_: stop.set())
            connect = dict(side_effect=ProtocolError(reason)) if reason == 'relay_invalid_upgrade' else dict(return_value=ws)
            with patch.object(relay.WebSocket, 'connect', **connect), patch.object(stop, 'wait', wait):
                self.assertEqual(relay.run(m, stop), 0)
            wait.assert_called_once()
            self.assertGreaterEqual(wait.call_args.args[0], 1)
            self.assertLess(wait.call_args.args[0], 2)
            self.assertEqual(relay.status(m)['error'], reason)

    def start(self, m):
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1]/'src'))
        proc = subprocess.Popen([sys.executable, '-m', 'coordkit.cli', '--manifest', str(m.file), 'relay', 'run'], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.stop, proc)
        return proc

    def stop(self, proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(12)
            except subprocess.TimeoutExpired:
                proc.kill()
        out, err = proc.communicate(timeout=5)
        self.assertNotIn(TOKEN.encode(), out + err)

    def until(self, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail('timed out waiting for local relay')

    def test_two_sites_exchange_wake_delivery_reconnect_and_replay(self):
        server = FakeRelay()
        self.addCleanup(server.close)
        server.duplicate = True
        server.drop_accept_for.add('a')
        a, b = self.site('a', server.url), self.site('b', server.url)
        self.post(a, 'from-a')
        pa, pb = self.start(a), self.start(b)
        rows = lambda m: bus.read_path(m.paths.bus_log)
        self.until(lambda: any(r['id'] == 'from-a' for r in rows(b)))
        self.post(b, 'from-b')
        self.until(lambda: any(r['id'] == 'from-b' for r in rows(a)))
        # Bus delivery consumes the local rows; relay carries no reader cursors.
        bus.configure(b)
        self.assertTrue(any(r['id'] == 'from-a' for r in bus.read() if bus.for_lane(r, 'worker')))
        self.assertFalse((b.paths.bus_log.parent/'state/cursors').exists())
        self.stop(pb)
        self.post(a, 'while-offline')
        self.until(lambda: len(server.log) == 3)
        pb = self.start(b)
        self.until(lambda: any(r['id'] == 'while-offline' for r in rows(b)))
        server.disconnect('a')
        self.post(b, 'after-disconnect')
        self.until(lambda: any(r['id'] == 'after-disconnect' for r in rows(a)))
        self.until(lambda: relay.status(a).get('pending_rows') == 0 and relay.status(b).get('pending_rows') == 0)
        self.assertEqual(len(rows(a)), 4)
        self.assertEqual(len(rows(b)), 4)
        self.assertEqual(len(server.log), 4)
        self.assertGreaterEqual(relay.status(a)['suppressed_rows'], 1)
        self.assertIn('WARN suppressed_rows', relay.health(a))
        self.assertIsNone(pa.poll())
        self.assertIsNone(pb.poll())

    def test_retention_gap_from_server_stops_without_appending_or_advancing(self):
        server = FakeRelay()
        self.addCleanup(server.close)
        server.log = [self.frame(1), self.frame(2, 1, 'two')]
        server.retain = 1
        m = self.site(url=server.url)
        proc = self.start(m)
        self.assertEqual(proc.wait(10), 1)
        self.assertEqual(relay.status(m)['error'], 'relay_retention_gap')
        self.assertFalse(m.paths.bus_log.exists())
        store = relay.Store(m)
        try:
            self.assertEqual(store.get('after', 0), 0)
        finally:
            store.close()

    def test_upgrade_auth_failure_is_terminal_and_redacted(self):
        server = FakeRelay()
        self.addCleanup(server.close)
        m = self.site(url=server.url)
        m.path(m.relay.secret_file).write_text('wrong-' + 'z' * 40)
        proc = self.start(m)
        self.assertEqual(proc.wait(10), 1)
        self.assertEqual(server.auth_failures, 1)
        self.assertEqual(relay.status(m)['error'], 'relay_auth_failed')


