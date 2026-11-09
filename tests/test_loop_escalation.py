"""A continuing failure streak must remain visible even when the bus is broken."""
import contextlib
import fcntl
import io
import threading
from unittest.mock import patch

from coordkit import loop
from coordkit.manifest import ManifestError, load
from coordkit.state import read_json, write_json
from tests.support import BASE, FakeCase


class LoopEscalationTests(FakeCase):
    def setUp(self):
        super().setUp()
        self.state = self.manifest.paths.root/'loops/audit'
        self.state.mkdir(parents=True)
        self.manifest.loop.fail_report_after = 1

    def record_failure(self, at, detail='runner failed'):
        settings, handle = loop.config(self.manifest, 'audit')
        with patch.object(loop.time, 'time', return_value=at), contextlib.redirect_stderr(io.StringIO()):
            loop.record_failure(self.manifest, 'audit', settings, handle, self.state, detail)
        return read_json(self.state/'escalation.json')

    def asks(self):
        return [c['args'] for c in self.calls() if c['tool'] == 'anx' and 'ask' in c['args']]

    def test_realerts_after_one_hour_six_hours_then_daily_across_restarts(self):
        self.assertEqual(self.record_failure(1000)['next_at'], 4600)
        self.record_failure(4599)
        self.assertEqual(len(self.asks()), 1)
        for count, at, next_at in ((2, 4600, 26200), (3, 26200, 112600), (4, 112600, 199000)):
            self.manifest = load(self.file)  # no in-memory timer or latch required
            report = self.record_failure(at, 'runner still failed')
            self.assertEqual(report['sent_count'], count)
            self.assertEqual(report['next_at'], next_at)
            self.record_failure(next_at - 1)
            self.assertEqual(len(self.asks()), count)
        self.assertIn('7 consecutive cycles', self.asks()[-1][self.asks()[-1].index('--situation') + 1])
        self.assertTrue(all('--again' in args for args in self.asks()))

    def test_custom_intervals_and_card_bypass_agent_digest_and_broken_bus(self):
        self.manifest.loop.loops = {'audit': {'receipt_prefix': 'checks-', 'failure_realert_intervals': ['5m', '2h']}}
        self.manifest.escalation.internal_to = 'relay'
        self.manifest.addressed.relay = 'relay'
        self.manifest.addressed.lead_escalations = False
        with patch.object(loop, '_post', side_effect=AssertionError('bus must not be needed')):
            self.assertEqual(self.record_failure(1000)['next_at'], 1300)
            self.assertEqual(self.record_failure(1300)['next_at'], 8500)
            self.assertEqual(self.record_failure(8500)['next_at'], 15700)
        for args in self.asks():
            self.assertEqual(args[args.index('--subject-ref') + 1], 'card:checks-audit')
        self.assertFalse(self.manifest.paths.bus_log.exists())
        self.assertEqual(self.escalations(), [])

    def test_failed_reminder_retains_payload_and_does_not_advance_timer(self):
        self.record_failure(1000)
        self.respond({'tool': 'anx', 'contains': ['ask'], 'exit': 2, 'stderr': 'ANX unavailable'})
        report = self.record_failure(4600)
        self.assertFalse(report['sent'])
        self.assertEqual(report['sent_count'], 1)
        self.assertNotIn('next_at', report)
        self.record_failure(4700, 'another error')
        self.assertEqual(read_json(self.state/'escalation.json'), report)
        self.respond()
        with patch.object(loop.time, 'time', return_value=4800):
            loop.retry_escalation(self.manifest, 'audit', 'loop-audit', self.state)
        saved = read_json(self.state/'escalation.json')
        self.assertTrue(saved['sent'])
        self.assertEqual(saved['sent_count'], 2)
        self.assertEqual(saved['next_at'], 26400)
        self.assertEqual(self.asks()[-1], self.asks()[-2])

    def test_legacy_agent_only_sent_receipt_is_delivered_to_decide(self):
        self.record_failure(1000)
        old = read_json(self.state/'escalation.json')
        for key in ('channel', 'sent_count', 'sent_at', 'next_at', 'ask_id'):
            old.pop(key)
        write_json(self.state/'escalation.json', old)
        with patch.object(loop.time, 'time', return_value=1001):
            loop.retry_escalation(self.manifest, 'audit', 'loop-audit', self.state)
        saved = read_json(self.state/'escalation.json')
        self.assertEqual(saved['channel'], 'anx')
        self.assertEqual(saved['sent_count'], 1)
        self.assertEqual(saved['next_at'], 4601)
        self.assertEqual(len(self.asks()), 2)

    def test_unsent_legacy_report_survives_a_failed_retry_and_new_cycle_error(self):
        self.respond({'tool': 'anx', 'contains': ['ask'], 'exit': 2, 'stderr': 'ANX unavailable'})
        old = self.record_failure(1000, 'original error')
        old.pop('channel')
        write_json(self.state/'escalation.json', old)
        self.assertEqual(self.record_failure(2000, 'different error'), old)
        self.respond()
        with patch.object(loop.time, 'time', return_value=3000):
            loop.retry_escalation(self.manifest, 'audit', 'loop-audit', self.state)
        self.assertIn('original error', self.asks()[-1][self.asks()[-1].index('--situation') + 1])

    def test_concurrent_success_cannot_clear_threshold_report_before_send_attempt(self):
        # Queue a successful-cycle reset as soon as the failure report is durable.
        # If the sender drops its lock and reacquires, let recovery win that gap.
        original_flock, original_write = fcntl.flock, loop.write_json
        recovered, errors, workers = threading.Event(), [], []
        writer = threading.get_ident()
        acquisitions = 0

        def recover():
            try:
                with (self.state/'result.lock').open('a') as lock:
                    original_flock(lock, fcntl.LOCK_EX)
                    (self.state/'escalation.json').unlink(missing_ok=True)
                    (self.state/'fails').write_text('0\n')
            except Exception as exc:
                errors.append(exc)
            finally:
                recovered.set()

        def journal(path, report):
            original_write(path, report)
            if not report.get('sent') and not workers:
                workers.append(threading.Thread(target=recover))
                workers[0].start()

        def lock_with_recovery_priority(fd, operation):
            nonlocal acquisitions
            if threading.get_ident() == writer:
                acquisitions += 1
                if acquisitions == 2:
                    self.assertTrue(recovered.wait(2), 'recovery could not acquire the released result lock')
            return original_flock(fd, operation)

        with patch.object(loop, 'write_json', side_effect=journal), \
                patch.object(loop.fcntl, 'flock', side_effect=lock_with_recovery_priority):
            self.record_failure(1000)
        workers[0].join(2)
        self.assertFalse(workers[0].is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(self.asks()), 1, 'the threshold-crossing failure must attempt delivery before reset')

    def test_success_clears_failed_delivery_and_next_streak_starts_fresh(self):
        self.respond({'tool': 'anx', 'contains': ['ask'], 'exit': 2, 'stderr': 'ANX unavailable'})
        self.record_failure(1000)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(loop.run_cycle(self.manifest, 'audit'), 0)
        self.assertFalse((self.state/'escalation.json').exists())
        self.assertEqual((self.state/'fails').read_text().strip(), '0')
        self.respond()
        self.assertEqual(self.record_failure(2000)['sent_count'], 1)
        self.assertEqual(self.record_failure(2001)['next_at'], 5600)

    def test_success_when_reminder_is_due_does_not_send_stale_alert(self):
        self.record_failure(1000)
        with patch.object(loop.time, 'time', return_value=4600), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(loop.run_cycle(self.manifest, 'audit'), 0)
        self.assertEqual(len(self.asks()), 1)
        self.assertFalse((self.state/'escalation.json').exists())

    def test_stop_suppresses_overdue_reminders(self):
        self.record_failure(1000)
        before = (self.state/'escalation.json').read_bytes()
        (self.state/'STOP').touch()
        with patch.object(loop.time, 'time', return_value=4600), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(loop.run_cycle(self.manifest, 'audit'), 0)
        self.assertEqual(len(self.asks()), 1)
        self.assertEqual((self.state/'escalation.json').read_bytes(), before)

    def test_named_prs_keep_the_evidence_required_by_anx_lint(self):
        self.record_failure(1000, 'Hold PR 123 could not be mirrored')
        args = self.asks()[0]
        self.assertIn('--evidence', args)
        self.assertEqual(args[args.index('--evidence') + 1], 'PR #123=https://github.com/owner/repo/pull/123')

    def test_realert_manifest_validation_and_inheritance(self):
        self.file.write_text(BASE + '\n[loop]\nfailure_realert_intervals = ["10m", "2h"]\n'
                             '[loop.loops.audit]\nfailure_realert_intervals = ["30m", "1d"]\n')
        manifest = load(self.file)
        self.assertEqual(loop.config(manifest, 'other')[0].failure_realert_intervals, ['10m', '2h'])
        self.assertEqual(loop.config(manifest, 'audit')[0].failure_realert_intervals, ['30m', '1d'])
        for section in ('loop', 'loop.loops.audit'):
            for value in ('[]', '["1h", "5m"]', '["0s"]', '["bad"]', '[1]', '"1h"'):
                self.file.write_text(BASE + f'\n[{section}]\nfailure_realert_intervals = {value}\n')
                with self.subTest(section=section, value=value), self.assertRaises(ManifestError):
                    load(self.file)
