"""Lane audits: bounded private inputs, strict results, publication and recovery."""
import contextlib
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from coordkit import audit, audit_digest, bus, wake
from coordkit.adapters import hermes, hermes_turn
from coordkit.cli import main
from coordkit.manifest import ManifestError, load
from coordkit.state import read_json, write_json
from tests.test_hermes import HermesCase, row

GAP = dict(kind='commitment', item='Check the build', evidence='I will check the build', when='')
FAKE = '''import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
cfg = json.loads(Path(sys.argv[0] + '.json').read_text())
record = dict(args=args)
if args[0] == 'delegate':
    record['prompt'] = sys.stdin.read()
    record['cwd'] = os.getcwd()
    record['mode'] = Path.cwd().stat().st_mode & 0o777
    record['digest'] = Path('digest.txt').read_text()
    record['state'] = Path('state.txt').read_text()
    record['request'] = json.loads(Path(args[args.index('--request-file') + 1]).read_text())
with open(cfg['log'], 'a') as stream:
    stream.write(json.dumps(record) + '\\n')
if args[0] == 'await':
    time.sleep(cfg.get('sleep', 0))
    print(cfg.get('reply', '[]'))
else:
    print(json.dumps(dict(schema_version=1, ok=True, result=dict(id='exec-audit'))))
'''


class ScrubTests(unittest.TestCase):
    def test_common_shapes_and_overlapping_spans(self):
        secrets = ['sk-' + 'a' * 24, 'ghp_' + 'a' * 30, 'github_pat_' + 'a' * 30,
                   'xoxb-' + 'a' * 24, 'AKIA' + 'A' * 16, 'AIza' + 'a' * 35,
                   'eyJaaaa.bbb.ccc', 'Bearer ' + 'a' * 24,
                   'Authorization: Bearer hidden', 'API_KEY=hidden', 'TOKEN="hidden value"',
                   "SECRET='hidden value'", 'PASSWORD=hidden', 'service_token: hidden', '"TOKEN": "hidden"',
                   '-----BEGIN RSA PRIVATE KEY-----\nprivate\n-----END RSA PRIVATE KEY-----']
        for secret in secrets:
            with self.subTest(secret=secret):
                clean, count = audit_digest.scrub('before\n' + secret + '\nafter')
                self.assertEqual(count, 1)
                self.assertIn('[REDACTED]', clean)
                self.assertNotIn(secret, clean)
        self.assertEqual(audit_digest.scrub('A commitment with no secrets'), ('A commitment with no secrets', 0))
        clean, count = audit_digest.scrub('-----BEGIN PRIVATE KEY-----\nunfinished')
        self.assertEqual((clean, count), ('[REDACTED]', 1))


class AuditCase(HermesCase):
    def setUp(self):
        super().setUp()
        self.state = self.root/'lane-state.md'
        self.state.write_text('Current state\n')
        self.file.write_text(self.file.read_text() + f'state_file = {json.dumps(str(self.state))}\n')
        self.reload()
        self.transcript = self.root/'transcript.jsonl'
        self.transcript.write_text('')
        self.agent = self.root/'fakes/agentctl'
        self.agent.write_text('#!' + sys.executable + ' -S\n' + FAKE)
        self.agent.chmod(0o755)
        self.audit_calls = self.root/'audit-calls.jsonl'
        self.reply()
        env = patch.dict(os.environ, AGENTCTL='agentctl', PATH=str(self.agent.parent) + os.pathsep + os.environ['PATH'])
        env.start(); self.addCleanup(env.stop)

    def reload(self):
        self.manifest = load(self.file)
        self.lane = self.manifest.lanes[0]
        bus.configure(self.manifest)

    def reply(self, reply='[]', **kwargs):
        Path(str(self.agent) + '.json').write_text(json.dumps(dict(log=str(self.audit_calls), reply=reply, **kwargs)))

    def messages(self, *texts):
        with self.transcript.open('a') as stream:
            for text in texts:
                stream.write(json.dumps(dict(type='assistant', timestamp='2030-01-01T00:00:00Z',
                                             message=dict(content=[dict(type='text', text=text)]))) + '\n')

    def run_audit(self, **kwargs):
        return audit.run(self.manifest, 'ops', transcript=self.transcript, **kwargs)

    def calls_for_audit(self):
        return [json.loads(line) for line in self.audit_calls.read_text().splitlines()] if self.audit_calls.exists() else []

    def bus_rows(self):
        return [json.loads(line) for line in self.manifest.paths.bus_log.read_text().splitlines()]

    def store(self):
        path = Path(self.lane.config_dir)/'state.db'
        with contextlib.closing(sqlite3.connect(path)) as connection, connection:
            connection.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, tool_calls TEXT, timestamp REAL)')
            connection.executemany('INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)', [
                (1, 'sess-1', 'user', 'Please check the build', None, 1),
                (2, 'sess-1', 'assistant', 'I will check the build', None, 2),
                (3, 'other', 'assistant', 'Other lane private text', None, 3)])
        return path


class AuditTests(AuditCase):
    def test_no_gaps_no_row_private_scrubbed_inputs_and_receipt(self):
        self.messages('I will check the build\nTOKEN=hidden')
        self.state.write_text('PASSWORD=hidden\nCurrent state')
        receipt = self.run_audit()
        self.assertEqual(receipt['gap_count'], 0)
        self.assertEqual(receipt['execution_id'], 'exec-audit')
        self.assertEqual(receipt['removed_spans'], 1)
        self.assertEqual(receipt['state_removed_spans'], 1)
        self.assertEqual(self.bus_rows(), [])
        self.assertIn('no gaps', self.state.with_name('state-gaps.md').read_text())
        delegate, waited = self.calls_for_audit()
        self.assertEqual(delegate['mode'], 0o700)
        self.assertNotIn('hidden', delegate['digest'] + delegate['state'])
        self.assertEqual(delegate['request']['selector'], {'pool': 'plan', 'settings': {'access': 'read_only'}})
        self.assertEqual(delegate['prompt'], audit.PROMPT)
        self.assertFalse(Path(delegate['cwd']).exists())
        self.assertEqual(waited['args'][0], 'await')
        self.assertEqual(read_json(audit.path_for(self.manifest, self.lane, '.receipt.json')), receipt)

    def test_gap_posts_exactly_one_row_even_after_post_commit_crash(self):
        self.messages('I will check the build')
        self.reply(json.dumps([GAP]))
        real = audit.write_json
        def interrupted(path, value):
            if path.name.endswith('.receipt.json'):
                raise OSError('interrupted after append')
            return real(path, value)
        with patch.object(audit, 'write_json', side_effect=interrupted), self.assertRaises(OSError):
            self.run_audit()
        self.assertFalse(audit.path_for(self.manifest, self.lane, '.receipt.json').exists())
        self.assertTrue(audit.path_for(self.manifest, self.lane, '.result.json').exists())
        receipt = self.run_audit()
        posted, = self.bus_rows()
        self.assertEqual((posted['kind'], posted['frm'], posted['to']), ('handoff', 'coordkit', 'ops'))
        self.assertEqual(posted['text'], '1 open items in your transcript are not in your state file')
        self.assertEqual(posted['ref'], str(self.state.with_name('state-gaps.md')))
        self.assertEqual(receipt['gap_count'], 1)
        self.assertEqual(len(self.calls_for_audit()), 2)  # retry publishes its persisted result
        self.assertEqual(audit.health(self.manifest), 'audits healthy (scheduling off)')

    def test_rows_arriving_during_audit_are_handed_to_a_turn_after_locks_close(self):
        self.messages('will do')
        real = audit.auditor
        def arrived(*args):
            self.assertTrue(hermes.turn_running(self.lane))
            hermes.request_turn(self.lane)
            return real(*args)
        def wake_after_unlock(manifest, name, rows, key):
            self.assertFalse(hermes.turn_running(self.lane))
            with audit.lock(audit.path_for(manifest, self.lane, '.lock')) as held:
                self.assertIsNotNone(held)
            return {'status': 'woken'}
        with patch.object(audit, 'auditor', side_effect=arrived), patch.object(hermes.Hermes, 'wake', side_effect=wake_after_unlock) as wake_turn:
            self.run_audit()
            wake_turn.assert_called_once()
        self.assertEqual(self.bus_rows(), [])  # the queued work needs a turn, not an invented row

    def test_prior_gaps_remain_input_until_reconciled_by_an_audit(self):
        self.messages('I will check the build')
        self.reply(json.dumps([GAP]))
        self.run_audit()
        self.reply('[]')
        self.run_audit()
        self.assertIn('Prior audit gaps', self.calls_for_audit()[2]['digest'])
        self.assertIn(GAP['item'], self.calls_for_audit()[2]['digest'])
        self.assertEqual(len(self.bus_rows()), 1)
        self.assertIn('no gaps', self.state.with_name('state-gaps.md').read_text())

    def test_contract_failure_changes_no_cursor_gap_file_or_rows_and_is_red(self):
        self.messages('I will check the build')
        prior = self.run_audit()
        gap_before = self.state.with_name('state-gaps.md').read_text()
        self.messages('A new commitment')
        invalid = ['No gaps.', '```json\n[]\n```', '{}', '[null]', '[{"kind":"commitment"}]',
                   '[{"kind":"a","kind":"b","item":"x","evidence":"x","when":""}]',
                   json.dumps([dict(GAP, when=7)]), '[] trailing']
        for reply in invalid:
            with self.subTest(reply=reply):
                self.reply(reply)
                with self.assertRaisesRegex(ManifestError, 'audit_contract_failed'):
                    self.run_audit()
                self.assertEqual(read_json(audit.path_for(self.manifest, self.lane, '.receipt.json')), prior)
                self.assertEqual(self.state.with_name('state-gaps.md').read_text(), gap_before)
                self.assertEqual(self.bus_rows(), [])
                with self.assertRaisesRegex(ManifestError, 'audit_failed'):
                    audit.health(self.manifest)
        self.assertIn('cancel', [call['args'][0] for call in self.calls_for_audit()])

    def test_dry_run_and_opt_in_refusal_even_with_state_override(self):
        self.messages('will do')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(['--manifest', str(self.file), 'audit-state', 'ops', '--transcript',
                                   str(self.transcript), '--dry-run', '--json']), 0)
        result = json.loads(out.getvalue())
        self.assertTrue(result['dry_run'])
        self.assertIn('prompt', result)
        self.assertIn('will do', result['digest'])
        self.assertEqual(result['state'], self.state.read_text())
        self.assertEqual(self.calls_for_audit(), [])
        self.assertFalse(audit.path_for(self.manifest, self.lane, '.receipt.json').exists())
        self.assertFalse(self.state.with_name('state-gaps.md').exists())
        self.lane.state_file = ''
        with self.assertRaisesRegex(ManifestError, 'state_file_missing'):
            self.run_audit(state=self.state)

    def test_timeout_cancels_owned_execution_and_advances_nothing(self):
        self.messages('will do')
        self.manifest.audit.timeout = '1s'
        self.reply(sleep=3)
        start = time.monotonic()
        with self.assertRaises(Exception):
            self.run_audit()
        self.assertLess(time.monotonic() - start, 3)
        self.assertEqual(self.calls_for_audit()[-1]['args'][0], 'cancel')
        self.assertEqual(self.bus_rows(), [])
        self.assertFalse(audit.path_for(self.manifest, self.lane, '.receipt.json').exists())

    def test_manifest_validation_and_explicit_timeout_selector(self):
        original = self.file.read_text()
        for extra in ('state_file = "relative.md"', 'rotate_turns = true', 'rotate_turns = 0',
                      'unknown_audit_key = "x"'):
            text = original if not extra.startswith('state_file') else original[:original.index('state_file =')]
            self.file.write_text(text + extra + '\n')
            with self.subTest(extra=extra), self.assertRaises(ManifestError):
                load(self.file)
        for extra in ('[audit]\nunknown = "x"', '[audit]\ninterval = "never"',
                      '[audit]\nselector = {}', '[audit]\nselector = {pool="plan", host="local"}'):
            self.file.write_text(original + extra + '\n')
            with self.subTest(extra=extra), self.assertRaises(ManifestError):
                load(self.file)
        self.file.write_text(original + '[audit]\ntimeout = "2m"\nselector = {pool="review"}\n')
        self.reload()
        self.manifest.agentctl.timeout = '1h'
        self.run_audit()
        args = self.calls_for_audit()[0]['args']
        self.assertEqual(args[args.index('--timeout') + 1], '2m')
        self.assertEqual(self.calls_for_audit()[0]['request']['selector']['pool'], 'review')

    def test_state_cannot_be_the_gap_output_file(self):
        reserved = self.state.with_name('state-gaps.md')
        reserved.write_text('Keep durable state')
        with self.assertRaisesRegex(ManifestError, 'state_file_reserved'):
            self.run_audit(state=reserved)
        self.assertEqual(reserved.read_text(), 'Keep durable state')
        self.assertEqual(self.calls_for_audit(), [])

    def test_shadow_refuses_audit_and_does_not_schedule(self):
        self.manifest.mode = 'shadow'
        with self.assertRaisesRegex(ManifestError, 'shadow_mode_read_only'):
            self.run_audit()
        self.manifest.audit.interval = '24h'
        self.assertIsNone(audit.schedule(self.manifest))


class DigestTests(AuditCase):
    def test_bound_newest_wins_and_cursor_only_consumes_complete_records(self):
        self.messages('old item', *('x' * 2000 for _ in range(60)), 'newest item')
        digest, cursor, stats = audit_digest.build(self.lane, transcript=self.transcript)
        self.assertLessEqual(len(digest.encode()), audit_digest.MAX_DIGEST)
        self.assertTrue(stats['truncated'])
        self.assertNotIn('old item', digest)
        self.assertIn('newest item', digest)
        end = self.transcript.stat().st_size
        with self.transcript.open('a') as stream:
            stream.write('{"type":"user","message":{"content":"partial')
        _, again, _ = audit_digest.build(self.lane, cursor, self.transcript)
        self.assertEqual(again['offset'], end)
        with self.transcript.open('a') as stream:
            stream.write(' completed"}}\n')
        digest, final, _ = audit_digest.build(self.lane, again, self.transcript)
        self.assertIn('partial completed', digest)
        self.assertNotIn('newest item', digest)
        self.assertEqual(final['offset'], self.transcript.stat().st_size)

    def test_prior_gaps_survive_when_new_transcript_fills_the_digest(self):
        self.messages('old item', *('x' * 2000 for _ in range(60)), 'newest item')
        digest, _, stats = audit_digest.build(self.lane, transcript=self.transcript, carry=[GAP])
        self.assertLessEqual(len(digest.encode()), audit_digest.MAX_DIGEST)
        self.assertTrue(stats['truncated'])
        self.assertIn('Prior audit gaps', digest)
        self.assertIn(GAP['item'], digest)
        self.assertIn(GAP['evidence'], digest)
        self.assertIn('newest item', digest)
        self.assertLess(digest.index('Prior audit gaps'), digest.index('newest item'))
        self.assertNotIn('old item', digest)

    def test_a_carry_larger_than_the_digest_is_kept_ahead_of_transcript(self):
        self.messages('newest item')
        with patch.object(audit_digest, 'MAX_DIGEST', 1024):
            digest, _, stats = audit_digest.build(self.lane, transcript=self.transcript,
                                                  carry=[dict(GAP, evidence='e' * 5000)])
        self.assertLessEqual(len(digest.encode()), 1024)
        self.assertTrue(stats['truncated'])
        self.assertIn('Prior audit gaps', digest)
        self.assertNotIn('newest item', digest)

    def test_an_oversized_newest_complete_record_fails_instead_of_being_empty(self):
        self.messages('x' * 8000)
        with patch.object(audit_digest, 'MAX_READ', 4096), self.assertRaisesRegex(ManifestError, 'record_too_large'):
            audit_digest.build(self.lane, transcript=self.transcript)

    def test_an_oversized_incomplete_tail_is_not_consumed(self):
        self.transcript.write_text('{"type":"user","message":{"content":"' + 'x' * 8000)
        with patch.object(audit_digest, 'MAX_READ', 4096):
            digest, cursor, stats = audit_digest.build(self.lane, transcript=self.transcript)
        self.assertEqual(cursor['offset'], 0)
        self.assertTrue(stats['truncated'])

    def test_replaced_truncated_and_rewritten_sources_reset_cursor(self):
        self.messages('old')
        _, cursor, _ = audit_digest.build(self.lane, transcript=self.transcript)
        self.transcript.write_text('')
        self.messages('replacement content')
        digest, _, _ = audit_digest.build(self.lane, cursor, self.transcript)
        self.assertIn('replacement content', digest)
        self.transcript.unlink()
        self.messages('different file')
        digest, _, _ = audit_digest.build(self.lane, cursor, self.transcript)
        self.assertIn('different file', digest)

    def test_user_assistant_tools_timestamps_and_no_tool_output(self):
        entries = [dict(type='user', timestamp='2030-01-01', message=dict(content='Please check')),
                   dict(type='assistant', message=dict(content=[dict(type='tool_use', name='shell',
                        input={'command': 'TOKEN=hidden ' + 'x' * 2000})])),
                   dict(role='assistant', tool_calls=[dict(function=dict(name='read', arguments='path.txt'))]),
                   dict(role='tool', content='tool result not part of digest')]
        self.transcript.write_text(''.join(json.dumps(entry) + '\n' for entry in entries))
        digest, _, stats = audit_digest.build(self.lane, transcript=self.transcript)
        self.assertIn('2030-01-01 user: Please check', digest)
        self.assertIn('Tool shell:', digest)
        self.assertIn('Tool read: path.txt', digest)
        self.assertNotIn('hidden', digest)
        self.assertNotIn('tool result', digest)
        self.assertNotIn('x' * 1001, digest)
        self.assertEqual(stats['removed_spans'], 1)

    def test_bounded_read_skips_oldest_and_bad_complete_rows_fail(self):
        self.messages(*('x' * 2000 for _ in range(100)), 'last')
        with patch.object(audit_digest, 'MAX_READ', 4096):
            digest, _, stats = audit_digest.build(self.lane, transcript=self.transcript)
        self.assertTrue(stats['truncated'])
        self.assertIn('last', digest)
        self.transcript.write_text('{invalid}\n')
        with self.assertRaisesRegex(ManifestError, 'audit_transcript_invalid'):
            audit_digest.build(self.lane, transcript=self.transcript)

    def test_hermes_store_scopes_session_and_cursor(self):
        path = self.store()
        hermes.remember_session(self.lane, 'sess-1')
        digest, cursor, _ = audit_digest.build(self.lane)
        self.assertIn('I will check the build', digest)
        self.assertNotIn('Other lane', digest)
        with contextlib.closing(sqlite3.connect(path)) as connection, connection:
            connection.execute('INSERT INTO messages VALUES (4, ?, ?, ?, NULL, 4)',
                               ('sess-1', 'assistant', 'New promise'))
        digest, cursor, _ = audit_digest.build(self.lane, cursor)
        self.assertNotIn('I will check', digest)
        self.assertIn('New promise', digest)
        self.assertEqual(cursor['message_id'], 4)

    def test_claude_pinned_and_latest_transcript_discovery(self):
        from coordkit.identity import transcript_dir
        self.lane.harness = 'claude'
        directory = transcript_dir(self.lane)
        directory.mkdir(parents=True)
        first, latest = directory/'first.jsonl', directory/'latest.jsonl'
        first.write_text('{"type":"user","message":{"content":"first"}}\n')
        latest.write_text('{"type":"user","message":{"content":"latest"}}\n')
        os.utime(first, (1, 1))
        digest, _, _ = audit_digest.build(self.lane)
        self.assertIn('latest', digest)
        self.lane.session_id = 'first'
        digest, _, _ = audit_digest.build(self.lane)
        self.assertIn('first', digest)


class SchedulingTests(AuditCase):
    def test_disabled_not_due_retired_and_turn_or_audit_locks_skip(self):
        self.assertIsNone(audit.schedule(self.manifest))
        self.manifest.audit.interval = '24h'
        with hermes.turn_lock(self.lane):
            self.assertIsNone(audit.schedule(self.manifest))
        with audit.lock(audit.path_for(self.manifest, self.lane, '.lock')):
            self.assertIsNone(audit.schedule(self.manifest))
        write_json(audit.path_for(self.manifest, self.lane, '.receipt.json'), {'ended_at': time.time()})
        self.assertIsNone(audit.schedule(self.manifest))
        with patch.object(audit, 'due', return_value=True), patch('coordkit.wake.retired', return_value=True):
            self.assertIsNone(audit.schedule(self.manifest))

    def test_detached_claim_survives_parent_close_and_two_scans_start_one(self):
        self.store()
        hermes.remember_session(self.lane, 'sess-1')
        self.file.write_text(self.file.read_text() + '[audit]\ninterval = "24h"\n')
        self.reload()
        self.reply(sleep=.5)
        real = subprocess.Popen
        children = []
        def launch(*args, **kwargs):
            proc = real(*args, **kwargs)
            children.append(proc)
            return proc
        try:
            with patch.object(audit.subprocess, 'Popen', side_effect=launch):
                self.assertEqual(audit.schedule(self.manifest), 'ops')
                self.manifest.lanes.append(replace(self.lane, name='second', profile='second'))
                self.assertIsNone(audit.schedule(self.manifest))  # even another due lane waits
                self.manifest.lanes.pop()
            self.assertEqual(children[0].wait(timeout=10), 0)
            self.assertIsNone(audit.schedule(self.manifest))
            self.assertEqual(len(self.calls_for_audit()), 2)
            self.assertEqual(read_json(audit.path_for(self.manifest, self.lane, '.status.json'))['status'], 'ok')
        finally:
            for proc in children:
                if proc.poll() is None:
                    os.killpg(proc.pid, 9)
                proc.wait()

    def test_child_rechecks_turn_lock_acquired_after_scan_and_claim_recovers(self):
        self.store()
        hermes.remember_session(self.lane, 'sess-1')
        held = hermes.turn_lock(self.lane)
        held.__enter__()
        real = subprocess.Popen
        children = []
        def launch(*args, **kwargs):
            proc = real(*args, **kwargs)
            children.append(proc)
            return proc
        self.manifest.audit.interval = '24h'
        try:
            # Simulate the scan probe seeing the lane just before the turn took its lock.
            with patch.object(hermes, 'turn_running', return_value=False), patch.object(audit.subprocess, 'Popen', side_effect=launch):
                self.assertEqual(audit.schedule(self.manifest), 'ops')
            self.assertEqual(children[0].wait(timeout=10), 1)
            self.assertEqual(self.calls_for_audit(), [])
            self.assertEqual(read_json(audit.path_for(self.manifest, self.lane, '.status.json'))['status'], 'failed')
            with self.assertRaisesRegex(ManifestError, 'audit_failed'):
                audit.health(self.manifest)
        finally:
            held.__exit__(None, None, None)
            for proc in children:
                if proc.poll() is None:
                    os.killpg(proc.pid, 9)
                proc.wait()
        with audit.lock(audit.directory(self.manifest)/'scan.lock') as claim:
            self.assertIsNotNone(claim)
        with audit.lock(audit.path_for(self.manifest, self.lane, '.lock')) as claim:
            self.assertIsNotNone(claim)

    def test_doctor_reports_failed_audit_and_does_not_expose_tool_output(self):
        write_json(audit.path_for(self.manifest, self.lane, '.status.json'), {'status': 'failed', 'at': time.time(), 'error': 'ToolError'})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(['--manifest', str(self.file), 'doctor']), 1)
        self.assertIn('DOCTOR audit red audit_failed:', out.getvalue())

    def test_crashed_claim_is_visible_and_reclaimable(self):
        self.manifest.audit.interval = '24h'
        now = time.time()
        write_json(audit.path_for(self.manifest, self.lane, '.status.json'), {'status': 'running', 'at': now - 600})
        with self.assertRaisesRegex(ManifestError, 'runner exited'):
            audit.health(self.manifest)
        self.assertTrue(audit.due(self.manifest, self.lane, now))
        with audit.lock(audit.directory(self.manifest)/'scan.lock'):
            self.assertIsNone(audit.schedule(self.manifest))

    def test_wake_scan_calls_schedule_and_dry_run_does_not(self):
        with patch.object(audit, 'schedule', return_value=None) as schedule, contextlib.redirect_stdout(io.StringIO()):
            wake.scan(self.manifest, dry_run=True)
            schedule.assert_not_called()
            wake.scan(self.manifest)
            schedule.assert_called_once_with(self.manifest)


class RotationTests(AuditCase):
    def test_rotates_after_completed_turns_and_next_prompt_starts_with_state(self):
        self.store()
        self.file.write_text(self.file.read_text() + 'rotate_turns = 2\n')
        self.reload()
        self.attach()
        self.post(row('one'))
        self.assertEqual(self.turn(), 0)
        self.assertEqual(hermes.session_id(self.lane), 'sess-1')
        self.assertEqual(self.calls_for_audit(), [])
        self.post(row('two'))
        self.assertEqual(self.turn(), 0)
        self.assertEqual(hermes.session_id(self.lane), '')
        receipt = hermes.last_receipt(self.lane)
        self.assertEqual((receipt['rotated'], receipt['audit_ok']), ('sess-1', True))
        self.post(row('three'))
        self.assertEqual(self.turn(), 0)
        argv = self.invocations()[-1]['argv']
        self.assertNotIn('--resume', argv)
        prompt = argv[argv.index('-z') + 1]
        self.assertTrue(prompt.startswith('Read your durable state first: ' + str(self.state)))
        self.assertIn(str(self.state.with_name('state-gaps.md')), prompt)
        self.assertEqual(len(self.calls_for_audit()), 2)

    def test_failed_audit_still_rotates_and_doctor_reports_failure(self):
        self.store()
        self.file.write_text(self.file.read_text() + 'rotate_turns = 1\n')
        self.reload()
        self.reply('not JSON')
        self.attach()
        self.post(row('one'))
        self.assertEqual(self.turn(), 0)
        self.assertEqual(hermes.session_id(self.lane), '')
        self.assertFalse(hermes.last_receipt(self.lane)['audit_ok'])
        with self.assertRaisesRegex(ManifestError, 'audit_failed'):
            audit.health(self.manifest)
        self.assertFalse(audit.path_for(self.manifest, self.lane, '.receipt.json').exists())

    def test_rotation_recovers_a_crash_between_receipt_and_session_clear(self):
        self.lane.rotate_turns = 2
        hermes.remember_session(self.lane, 'sess-1')
        hermes.append_receipt(self.lane, dict(ended_at=time.time(), rotated='sess-1', ok=True, usage={'api_calls': 0}))
        with patch.object(audit, 'run') as run:
            hermes_turn.rotate_session(self.manifest, self.lane)
            run.assert_not_called()
        self.assertEqual(hermes.session_id(self.lane), '')

    def test_rotation_counts_beyond_the_status_receipt_tail(self):
        self.lane.rotate_turns = 3
        hermes.remember_session(self.lane, 'sess-1')
        for index in range(3):
            hermes.append_receipt(self.lane, dict(ended_at=time.time(), session_id='sess-1', ok=True, usage={}, padding='x' * 1000))
        with patch.object(hermes, 'RECEIPT_TAIL', 1500), patch.object(audit, 'run') as run:
            hermes_turn.rotate_session(self.manifest, self.lane)
            run.assert_called_once_with(self.manifest, self.lane.name, turn_held=True)
        self.assertEqual(hermes.session_id(self.lane), '')

    def test_failed_and_skipped_turns_do_not_count(self):
        self.lane.rotate_turns = 2
        hermes.remember_session(self.lane, 'sess-1')
        for ok, skipped in ((False, ''), (True, 'nothing unread'), (True, '')):
            hermes.append_receipt(self.lane, dict(ended_at=time.time(), session_id='sess-1', ok=ok, skipped=skipped, usage={}))
        with patch.object(audit, 'run') as run:
            hermes_turn.rotate_session(self.manifest, self.lane)
            run.assert_not_called()
        self.assertEqual(hermes.session_id(self.lane), 'sess-1')
