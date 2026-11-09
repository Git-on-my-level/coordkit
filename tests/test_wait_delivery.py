"""A waiter acknowledges only after its receiver is flushed and durable."""
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

    def test_crash_after_receiver_sync_before_cursor_commit_replays(self):
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
bus.main(['wait', '--to', 'worker', '--timeout', '0'], load(sys.argv[1]))
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
        self.assertEqual(receiver.read_text().count('durable work'), 2)
        self.assertEqual(bus.get_cursor('worker', 'wait', None), 1)
