"""A waiter recovers delivered IDs and acknowledges only after receiver fsync."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from coordkit import bus
from coordkit.adapters import claude_resume
from tests.support import FakeCase


class WaitDeliveryTests(FakeCase):
    def test_own_ask_reply_wakes_by_default(self):
        self.seed()
        path = self.manifest.paths.bus_log
        path.write_text('\n'.join(json.dumps(row) for row in [
            dict(id='own', frm='worker', to='lead', kind='ask', ts='2026-01-01T00:00:00Z', text='question'),
            dict(id='reply', frm='lead', to='elsewhere', kind='fyi', re='own',
                 ts='2026-01-01T00:00:01Z', text='answer to own ask')])+'\n')
        bus.set_cursor('worker', 'wait', 1)
        with contextlib.redirect_stdout(io.StringIO()) as receiver, patch.object(
                claude_resume, 'start_time', return_value='started'):
            bus.main(['wait', '--to=worker', '--timeout=0'], self.manifest)
        self.assertIn('answer to own ask', receiver.getvalue())
        self.assertEqual(bus.get_cursor('worker', 'wait', None), 2)

    def seed(self):
        bus.configure(self.manifest)
        path = self.manifest.paths.bus_log
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(id='work', frm='lead', to='worker', kind='ask',
                                        ts='2026-01-01T00:00:00Z', text='durable work'))+'\n')
        bus.set_cursor('worker', 'wait', 0)

    def test_receiver_fsync_failure_does_not_acknowledge(self):
        self.seed()
        sync = os.fsync
        with (self.root/'receiver.log').open('w') as receiver:
            def fail(fd):
                if fd == receiver.fileno():
                    raise OSError('receiver sync failed')
                sync(fd)
            with contextlib.redirect_stdout(receiver), patch.object(bus.os, 'fsync', side_effect=fail), patch.object(
                    claude_resume, 'start_time', return_value='started'), self.assertRaisesRegex(OSError, 'receiver sync failed'):
                bus.main(['wait', '--to', 'worker', '--timeout', '0'], self.manifest)
        self.assertEqual(bus.get_cursor('worker', 'wait', None), 0)

    def test_crash_after_receiver_sync_before_cursor_commit_deduplicates(self):
        self.seed()
        code = '''import os, sys
from coordkit import bus
from coordkit.manifest import load
original = bus.set_cursor
def crash(name, kind, value):
    if value:
        os._exit(87)
    original(name, kind, value)
bus.set_cursor = crash
bus.main(['wait', '--to', 'worker', '--persistent', '--timeout', '0'], load(sys.argv[1]))
'''
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src'))
        receiver = self.root/'receiver.log'
        with receiver.open('w') as stream:
            child = subprocess.run([sys.executable, '-c', code, str(self.file)], env=env,
                                   stdout=stream, stderr=subprocess.PIPE, text=True, timeout=15)
        self.assertEqual(child.returncode, 87, child.stderr)
        self.assertIn('durable work', receiver.read_text())
        self.assertEqual(bus.get_cursor('worker', 'wait', None), 0)
        with receiver.open('a') as stream:
            again = subprocess.run([sys.executable, '-m', 'coordkit.cli', '--manifest', str(self.file),
                                    'bus', 'wait', '--to', 'worker', '--timeout', '0'], env=env,
                                   stdout=stream, stderr=subprocess.PIPE, text=True, timeout=15)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(receiver.read_text().count('durable work'), 1)
        self.assertEqual(bus.get_cursor('worker', 'wait', None), 1)

    def test_recovered_delivery_still_requires_receiver_sync(self):
        self.seed()
        receiver = self.root/'receiver.log'
        receiver.write_text(bus.line(bus.read()[0])+'\n')
        sync = os.fsync
        with receiver.open('a') as stream:
            def fail(fd):
                if fd == stream.fileno():
                    raise OSError('receiver sync failed')
                sync(fd)
            with contextlib.redirect_stdout(stream), patch.object(bus.os, 'fsync', side_effect=fail), patch.object(
                    claude_resume, 'start_time', return_value='started'), self.assertRaisesRegex(OSError, 'receiver sync failed'):
                bus.main(['wait', '--to', 'worker', '--timeout', '0'], self.manifest)
        self.assertEqual(receiver.read_text().count('durable work'), 1)
        self.assertEqual(bus.get_cursor('worker', 'wait', None), 0)

    def test_receiver_deduplicates_exact_ids_and_preserves_append_position(self):
        self.seed()
        row = bus.read()[0]
        receiver = self.root/'receiver.log'
        for full in (False, True):
            with self.subTest(full=full):
                receiver.write_text(bus.line(row, full=full)+'\n')
                with receiver.open('a') as stream, contextlib.redirect_stdout(stream):
                    bus.deliver_wait_hits([row, dict(row, id='work-next', text='next delivery')], full)
                self.assertEqual(receiver.read_text(), bus.line(row, full=full)+'\n'+
                                 bus.line(dict(row, id='work-next', text='next delivery'), full=full)+'\n')

    def test_receiver_tail_ignores_partial_records_and_older_ids(self):
        self.seed()
        row = bus.read()[0]
        record = bus.line(row)+'\n'
        receiver = self.root/'receiver.log'
        # The first case would look like a complete matching ID if the scan
        # accepted its partial prefix. An unterminated suffix is not a delivery.
        for content in ('x'+record, record+'padding\n'*20+'work unfinished'):
            with self.subTest(content=content):
                receiver.write_text(content)
                with receiver.open('a') as stream, contextlib.redirect_stdout(stream), patch.object(
                        bus, 'WAIT_RECEIVER_TAIL_BYTES', len(record.encode('utf-8'))):
                    bus.deliver_wait_hits([row], False)
                expected = content+('' if content.endswith('\n') else '\n')+record
                self.assertEqual(receiver.read_text(), expected)
                with receiver.open('a') as stream, contextlib.redirect_stdout(stream), patch.object(
                        bus, 'WAIT_RECEIVER_TAIL_BYTES', len(record.encode('utf-8'))):
                    bus.deliver_wait_hits([row], False)
                self.assertEqual(receiver.read_text(), expected)

    def test_message_id_in_multiline_prose_is_not_a_delivery(self):
        self.seed()
        row = bus.read()[0]
        previous = dict(row, id='previous', text='details\nwork is still pending')
        receiver = self.root/'receiver.log'
        receiver.write_text(bus.line(previous, full=True)+'\n')
        with receiver.open('a') as stream, contextlib.redirect_stdout(stream):
            bus.deliver_wait_hits([row], True)
        self.assertEqual(receiver.read_text().count('durable work'), 1)

    def test_each_delivery_is_durable_before_its_cursor_commit(self):
        self.seed()
        path = self.manifest.paths.bus_log
        second = dict(bus.read()[0], id='next', text='second delivery')
        with path.open('a') as stream:
            stream.write(json.dumps(second)+'\n')
        receiver = self.root/'receiver.log'
        original = bus.set_cursor
        def crash(name, kind, value):
            if value == 2:
                raise RuntimeError('cursor commit failed')
            original(name, kind, value)
        with receiver.open('a') as stream, contextlib.redirect_stdout(stream), patch.object(
                claude_resume, 'start_time', return_value='started'), patch.object(
                bus, 'set_cursor', side_effect=crash), self.assertRaisesRegex(RuntimeError, 'cursor commit failed'):
            bus.main(['wait', '--to', 'worker', '--timeout', '0'], self.manifest)
        self.assertEqual(bus.get_cursor('worker', 'wait', None), 1)
        with receiver.open('a') as stream, contextlib.redirect_stdout(stream), patch.object(
                claude_resume, 'start_time', return_value='started'):
            bus.main(['wait', '--to', 'worker', '--timeout', '0'], self.manifest)
        self.assertEqual(receiver.read_text().count('durable work'), 1)
        self.assertEqual(receiver.read_text().count('second delivery'), 1)
        self.assertEqual(bus.get_cursor('worker', 'wait', None), 2)
