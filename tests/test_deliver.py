"""Hook delivery: bounded output/reads, fail-open bootstrap, and reversible settings."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

from coordkit import deliver, hooks, questions
from coordkit.cli import main
from coordkit.state import write_json, read_json
from tests.test_addressed import AddressedCase


class DeliveryCase(AddressedCase):
    def setUp(self):
        super().setUp()
        self.payload = {'hook_event_name': 'PostToolUse', 'session_id': 's1'}
        write_json(questions.home(self.manifest)/'sessions.json', {'s1': {'name': 'lead-a'}})
        self.manifest.paths.bus_log.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.paths.bus_log.touch()
        # Most tests model already-attached listeners, before any rows were posted.
        stat = self.manifest.paths.bus_log.stat()
        for name in ('lead-a', 'worker-ui'):
            write_json(deliver.cursor_path(self.manifest, name),
                       {'file': [stat.st_dev, stat.st_ino], 'offset': 0})
        env = patch.dict(os.environ, {'COORDKIT_AS': ''})
        env.start()
        self.addCleanup(env.stop)

    def run_hook(self, payload=None):
        out = io.StringIO()
        self.assertEqual(deliver.run_hook(self.manifest, json.dumps(payload or self.payload), out), 0)
        return out.getvalue()


class DeliveryTests(DeliveryCase):
    def test_actionable_names_roles_owners_and_broadcast_only(self):
        for kind in ('ask', 'ruling', 'handoff', 'blocker'):
            self.incumbent_row(kind, 'x', 'lead-a', kind)
        self.incumbent_row('urgent', 'x', 'coordinator', 'msg', priority='urgent')
        self.incumbent_row('broadcast', 'x', 'all', 'ask')
        self.incumbent_row('owner', 'x', 'other', 'ruling', owner='lead-a', ref='/detail')
        for kind in ('fyi', 'msg', 'answer', 'state'):
            self.incumbent_row('ignored-' + kind, 'x', 'lead-a', kind, priority='urgent' if kind == 'fyi' else 'normal')
        self.incumbent_row('self', 'lead-a', 'lead-a')
        self.incumbent_row('other', 'x', 'other')
        before = self.manifest.paths.bus_log.read_bytes()
        output = self.run_hook()
        context = json.loads(output)['hookSpecificOutput']['additionalContext']
        for rid in ('ask', 'ruling', 'handoff', 'blocker', 'urgent', 'broadcast', 'owner'):
            # Remaining rows can spill into the following bounded batch.
            if f'--re {rid} ' not in context:
                context += json.loads(self.run_hook())['hookSpecificOutput']['additionalContext']
            self.assertIn(f'--from lead-a --re {rid}', context)
        self.assertIn('[/detail]', context)
        self.assertNotIn('ignored-', context)
        self.assertNotIn('--re self', context)
        self.assertEqual(self.run_hook(), '')
        self.assertEqual(self.manifest.paths.bus_log.read_bytes(), before)
        self.assertEqual(read_json(deliver.note_path(self.manifest, 'lead-a'))['via'], 'hook')

    def test_identity_env_then_binding_then_transcript_and_unknown(self):
        self.incumbent_row('row', 'x', 'worker-ui')
        with patch.dict(os.environ, {'COORDKIT_AS': 'worker-ui'}):
            self.assertIn('BUS ask row', self.run_hook())
        transcript = self.root/'session.jsonl'
        transcript.write_text(json.dumps({'type': 'user', 'message': {'role': 'user', 'content': 'Your bus name is worker-ui.'}}))
        payload = dict(self.payload, session_id='unbound', transcript_path=str(transcript))
        self.incumbent_row('next', 'x', 'worker-ui')
        self.assertIn('BUS ask next', self.run_hook(payload))
        self.assertEqual(self.run_hook(dict(payload, transcript_path='/missing')), '')
        self.assertFalse(deliver.cursor_path(self.manifest, 'unknown-unbound').exists())

    def test_output_budget_unicode_and_batch_cursor_never_skips(self):
        for i in range(20):
            self.incumbent_row(str(i), 'x', 'lead-a', text='😀' * 400, ref='/a')
        ids = []
        while output := self.run_hook():
            self.assertLessEqual(len(output.encode()), 1536)
            context = json.loads(output)['hookSpecificOutput']['additionalContext']
            ids.extend(line.split()[2] for line in context.splitlines() if line.startswith('BUS '))
        self.assertEqual(ids, list(map(str, range(20))))

    def test_partial_row_rotation_and_truncation(self):
        self.incumbent_row('old', 'x', 'lead-a')
        self.assertIn('BUS ask old', self.run_hook())
        path = self.manifest.paths.bus_log
        row = json.dumps(dict(id='partial', frm='x', to='lead-a', kind='ask', text='go')).encode()
        with path.open('ab') as stream:
            stream.write(row[:20])
        self.assertEqual(self.run_hook(), '')
        with path.open('ab') as stream:
            stream.write(row[20:] + b'\n')
        self.assertIn('BUS ask partial', self.run_hook())
        path.rename(path.with_suffix('.old'))
        self.incumbent_row('rotated', 'x', 'lead-a')
        self.assertIn('BUS ask rotated', self.run_hook())
        path.write_bytes(b'')
        self.assertEqual(self.run_hook(), '')
        self.incumbent_row('new', 'x', 'lead-a')
        self.assertIn('BUS ask new', self.run_hook())

    def test_fail_open_payload_bus_cursor_state_and_output_errors(self):
        initial = deliver.cursor_path(self.manifest, 'lead-a').read_bytes()
        for payload in ('broken', '[]', '{}'):
            out = io.StringIO()
            self.assertEqual(deliver.run_hook(self.manifest, payload, out), 0)
            self.assertEqual(out.getvalue(), '')
        self.incumbent_row('a', 'x', 'lead-a')
        with patch.object(deliver, 'write_json', side_effect=OSError('read only')):
            self.assertEqual(self.run_hook(), '')
        self.assertEqual(deliver.cursor_path(self.manifest, 'lead-a').read_bytes(), initial)
        out = io.StringIO()
        with patch.object(out, 'flush', side_effect=OSError('broken pipe')):
            deliver.run_hook(self.manifest, json.dumps(self.payload), out)
        self.assertEqual(deliver.cursor_path(self.manifest, 'lead-a').read_bytes(), initial)
        self.assertFalse(deliver.note_path(self.manifest, 'lead-a').exists())
        self.assertFalse(deliver.addressed.listening(self.manifest, 'lead-a')[0])
        self.assertFalse(deliver.note_path(self.manifest, 'lead-a').with_suffix('.pending').exists())
        self.assertIn('BUS ask a', self.run_hook())
        cursor = deliver.cursor_path(self.manifest, 'lead-a')
        old = cursor.read_bytes()
        self.manifest.paths.bus_log.write_text('{broken}\n')
        self.assertEqual(self.run_hook(), '')
        self.assertEqual(read_json(cursor)['offset'], len(b'{broken}\n'))
        cursor.write_text('{broken}')
        self.assertEqual(self.run_hook(), '')
        cursor.unlink()
        self.manifest.paths.bus_log.unlink()
        self.assertEqual(self.run_hook(), '')

    def test_cli_bootstrap_fail_open_missing_and_invalid_manifest(self):
        for text in (None, 'not toml!'):
            path = self.root/'invalid.toml'
            if text:
                path.write_text(text)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(main(['--manifest', str(path), 'deliver', '--hook']), 0)
            self.assertEqual((out.getvalue(), err.getvalue()), ('', ''))

    def test_hook_lock_is_nonblocking(self):
        cursor = deliver.cursor_path(self.manifest, 'lead-a')
        with deliver.addressed.locked(cursor.with_suffix('.hook.lock')):
            start = time.monotonic()
            self.assertEqual(self.run_hook(), '')
            self.assertLess(time.monotonic() - start, .3)

    def test_transcript_fallback_timing_is_bounded_for_giant_lines(self):
        transcript = self.root/'giant-transcript.jsonl'
        # User/tool payloads can occupy many megabytes on one transcript line.
        with transcript.open('wb') as stream:
            stream.write(b'{"type":"user","content":"' + b'x' * 8_000_000 + b'"}\n')
        payload = dict(self.payload, session_id='unbound', transcript_path=str(transcript))
        with patch('builtins.open', wraps=open) as opened:
            start = time.monotonic()
            self.assertEqual(self.run_hook(payload), '')
            self.assertLess(time.monotonic() - start, .3)
        opened.assert_called_once_with(str(transcript), 'rb')
        # A binding near the beginning remains available even on a giant line.
        with transcript.open('r+b') as stream:
            stream.write(b'{"type":"user","content":"Your bus name is lead-a. ')
        self.incumbent_row('prefix', 'x', 'lead-a')
        start = time.monotonic()
        self.assertIn('BUS ask prefix', self.run_hook(payload))
        self.assertLess(time.monotonic() - start, .3)

    def test_cold_cli_timing_large_log_and_tail_byte_offset(self):
        path = self.manifest.paths.bus_log
        # A large historical log must not be re-read once the byte cursor exists.
        with path.open('wb') as stream:
            stream.write(b'{"kind":"fyi"}\n' * 1_000_000)
        stat = path.stat()
        write_json(deliver.cursor_path(self.manifest, 'lead-a'), {'file': [stat.st_dev, stat.st_ino], 'offset': stat.st_size})
        self.incumbent_row('tail', 'x', 'lead-a')
        budget = float(os.environ.get('COORDKIT_COLD_CLI_BUDGET', '0.3'))
        self.assertTrue(0 < budget < float('inf'), 'cold CLI budget must be finite and positive')
        start = time.monotonic()
        result = subprocess.run([sys.executable, '-m', 'coordkit.cli', '--manifest', str(self.file), 'deliver', '--hook'],
                                input=json.dumps(self.payload), text=True, capture_output=True, timeout=max(1, budget + 0.5),
                                env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src')))
        elapsed = time.monotonic() - start
        self.assertEqual((result.returncode, result.stderr), (0, ''))
        self.assertIn('BUS ask tail', result.stdout)
        self.assertLess(elapsed, budget, f'cold hook took {elapsed:.3f}s (budget {budget:.3f}s)')
        # A first hook skips the large history and establishes the current EOF.
        deliver.cursor_path(self.manifest, 'lead-a').unlink()
        start = time.monotonic()
        self.assertEqual(self.run_hook(), '')
        self.assertLess(time.monotonic() - start, .3)
        self.assertEqual(read_json(deliver.cursor_path(self.manifest, 'lead-a'))['offset'], path.stat().st_size)

    def test_first_hook_for_eight_names_skips_history_and_delivers_only_new_rows(self):
        self.incumbent_row('historical', 'x', 'all', text='Do not replay this ask')
        end = self.manifest.paths.bus_log.stat().st_size
        for i in range(8):
            name = f'new-lane-{i}'
            with patch.dict(os.environ, {'COORDKIT_AS': name}):
                self.assertEqual(self.run_hook(), '')
            self.assertEqual(read_json(deliver.cursor_path(self.manifest, name))['offset'], end)
        self.incumbent_row('fresh', 'x', 'all', text='Deliver this new ask')
        for i in range(8):
            with patch.dict(os.environ, {'COORDKIT_AS': f'new-lane-{i}'}):
                output = self.run_hook()
                self.assertIn('BUS ask fresh', output)
                self.assertNotIn('historical', output)
                self.assertEqual(self.run_hook(), '')

    def test_settings_install_uninstall_receipt_and_unrelated_edits(self):
        target = self.root/'settings.json'
        before = {'hooks': {'PostToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'existing'}]}]}, 'model': 'x'}
        write_json(target, before)
        self.assertEqual(deliver.main(['install', '--settings', str(target)], self.manifest), 0)
        record = read_json(deliver.receipt_path(self.manifest, target))
        after = read_json(target)
        self.assertEqual(sum((hooks.entries(after)-hooks.entries(before)).values()), 3)
        self.assertEqual(record['before'], before)
        self.assertEqual(record['after'], after)
        deliver.install(self.manifest, target)  # owned installation is idempotent
        self.assertEqual(read_json(target), after)
        after['model'] = 'unrelated edit'
        write_json(target, after)
        deliver.uninstall(self.manifest, target)
        restored = read_json(target)
        self.assertEqual(hooks.entries(restored), hooks.entries(before))
        self.assertEqual(restored['model'], 'unrelated edit')
        deliver.uninstall(self.manifest, target)  # idempotent
        deliver.install(self.manifest, target)  # reinstall after receipt restoration

    def test_uninstall_conflict_preserves_settings_and_receipt(self):
        target = self.root/'settings.json'
        deliver.install(self.manifest, target)
        doc = read_json(target)
        doc['hooks']['PostToolUse'][0]['hooks'][0]['command'] = 'changed'
        write_json(target, doc)
        with self.assertRaisesRegex(Exception, 'rollback_conflict'):
            deliver.uninstall(self.manifest, target)
        self.assertEqual(read_json(target), doc)
        self.assertFalse(read_json(deliver.receipt_path(self.manifest, target))['restored'])

    def test_corrupt_complete_rows_advance_and_do_not_hide_following_delivery(self):
        path = self.manifest.paths.bus_log
        with path.open('ab') as stream:
            stream.write(b'broken\n[]\n\xff\n{"kind":"ask","to":"lead-a","frm":"x"}\n')
        self.incumbent_row('after-corrupt', 'x', 'lead-a')
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            self.assertIn('BUS ask after-corrupt', self.run_hook())
        self.assertIn('BUS_INVALID_JSON skipped=4', error.getvalue())
        self.assertEqual(read_json(deliver.cursor_path(self.manifest, 'lead-a'))['offset'], path.stat().st_size)
        with contextlib.redirect_stderr(io.StringIO()) as repeated:
            self.assertEqual(self.run_hook(), '')
        self.assertEqual(repeated.getvalue(), '')

    def test_bootstrap_surfaces_corrupt_row_warning(self):
        with self.manifest.paths.bus_log.open('ab') as stream:
            stream.write(b'broken\n')
        self.incumbent_row('following', 'x', 'lead-a')
        out, error = io.StringIO(), io.StringIO()
        with patch.object(sys, 'stdin', io.StringIO(json.dumps(self.payload))), contextlib.redirect_stdout(out), contextlib.redirect_stderr(error):
            self.assertEqual(main(['--manifest', str(self.file), 'deliver', '--hook']), 0)
        self.assertIn('BUS ask following', out.getvalue())
        self.assertIn('BUS_INVALID_JSON', error.getvalue())
