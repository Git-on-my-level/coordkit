"""Scheduled wakes: a timer file becomes one bus row when due, at least once."""
import contextlib
import io
import json
import time
from unittest.mock import patch

from coordkit import bus, timers, wake
from coordkit.cli import main
from coordkit.manifest import ManifestError
from tests.test_addressed import AddressedCase


class TimerTests(AddressedCase):
    def setUp(self):
        super().setUp()
        self.manifest.paths.bus_log.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.paths.bus_log.touch()

    def rows(self):
        return [json.loads(line) for line in self.manifest.paths.bus_log.read_text().splitlines()]

    def test_a_due_timer_becomes_one_row_and_an_early_one_waits(self):
        now = time.time()
        soon, _ = timers.schedule(self.manifest, 'lead-a', 'check   the 2h readback', after='10m', frm='lead-a', now=now)
        later, due = timers.schedule(self.manifest, 'lead-a', 'second look', at=timers.stamp(now + 7200), now=now)
        self.assertAlmostEqual(due, now + 7200, delta=1)
        self.assertEqual(timers.fire(self.manifest, now=now + 60), [])
        self.assertEqual(self.rows(), [])
        self.assertEqual(timers.fire(self.manifest, now=now + 601), [soon])
        row, = self.rows()
        self.assertEqual((row['id'], row['to'], row['frm'], row['kind'], row['origin']),
                         ('wake-at-' + soon, 'lead-a', 'coordkit', 'handoff', 'coordkit'))
        self.assertIn('Timer set by lead-a', row['text'])
        self.assertTrue(row['text'].endswith('check the 2h readback'))
        full = 'y' * (self.manifest.bus.text_cap - timers.PREFIX_ROOM)  # the longest accepted text arrives whole
        longest, _ = timers.schedule(self.manifest, 'lead-a', full, after='10m', frm='a-sender-with-a-long-name', now=now)
        timers.fire(self.manifest, now=now + 602)
        self.assertTrue(self.rows()[-1]['text'].endswith(full))
        self.assertLessEqual(len(self.rows()[-1]['text']), self.manifest.bus.text_cap)
        self.assertEqual(timers.fire(self.manifest, now=now + 700), [])  # fired once
        self.assertEqual([r['id'] for _, r in timers.pending(self.manifest)], [later])

    def test_a_crash_between_claim_and_append_fires_again_with_the_same_row_id(self):
        """Contract: at least once. A claimed timer is never silently dropped."""
        now = time.time()
        ident, _ = timers.schedule(self.manifest, 'lead-a', 'do not lose me', after='1m', now=now)
        other, _ = timers.schedule(self.manifest, 'coordinator', 'unaffected', after='1m', now=now)
        real = bus.append
        def append(recs, **kwargs):
            if recs[0]['to'] == 'lead-a':
                raise RuntimeError('crash')
            return real(recs, **kwargs)
        with patch.object(bus, 'append', side_effect=append), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(timers.fire(self.manifest, now=now + 61), [other])  # one failure does not stop the rest
        self.assertIn('WAKE_TIMER_FAILED ' + ident, err.getvalue())
        self.assertEqual([r['id'] for r in self.rows()], ['wake-at-' + other])
        self.assertEqual(timers.fire(self.manifest, now=now + 62), [])  # the claim is not stale yet
        self.assertEqual(timers.fire(self.manifest, now=now + 61 + timers.CLAIM_STALE), [ident])
        self.assertEqual([r['id'] for r in self.rows()], ['wake-at-' + other, 'wake-at-' + ident])

    def test_a_timer_that_keeps_failing_is_set_aside_and_can_be_cancelled(self):
        now = time.time()
        ident, _ = timers.schedule(self.manifest, 'lead-a', 'never lands', after='1m', now=now)
        with patch.object(bus, 'append', side_effect=RuntimeError('still broken')), \
                patch.object(timers, 'MAX_ATTEMPTS', 2), contextlib.redirect_stderr(io.StringIO()):
            timers.fire(self.manifest, now=now + 61)
            timers.fire(self.manifest, now=now + 61 + timers.CLAIM_STALE)
        self.assertTrue((timers.directory(self.manifest)/f'{ident}.failed').exists())
        self.assertEqual(timers.fire(self.manifest, now=now + 61 + 3 * timers.CLAIM_STALE), [])
        self.assertEqual(timers.cancel(self.manifest, ident), 'cancelled')
        self.assertEqual(timers.cancel(self.manifest, ident), 'absent')

    def test_an_unreadable_claim_is_set_aside_without_touching_other_timers(self):
        now = time.time()
        good, _ = timers.schedule(self.manifest, 'lead-a', 'fine', after='1m', now=now)
        broken = timers.directory(self.manifest)/'0123456789ab.firing'
        broken.write_text('{not json')
        import os
        os.utime(broken, (now - 2 * timers.CLAIM_STALE,) * 2)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(timers.fire(self.manifest, now=now + 61), [good])
        self.assertEqual(broken.with_suffix('.failed').read_text(), '{not json')
        self.assertEqual([r['id'] for r in self.rows()], ['wake-at-' + good])

    def test_cancel_refuses_a_timer_whose_row_is_being_posted(self):
        """Contract: cancel never reports success for a row that may still post."""
        now = time.time()
        ident, _ = timers.schedule(self.manifest, 'lead-a', 'in flight', after='1m', now=now)
        outcomes = []
        real = bus.append
        def append(recs, **kwargs):
            outcomes.append(timers.cancel(self.manifest, ident))
            return real(recs, **kwargs)
        with patch.object(bus, 'append', side_effect=append):
            self.assertEqual(timers.fire(self.manifest), [])  # not due yet
            self.assertEqual(timers.fire(self.manifest, now=time.time() + 61), [ident])
        self.assertEqual(outcomes, ['firing'])
        self.assertEqual([r['id'] for r in self.rows()], ['wake-at-' + ident])

    def test_a_shadow_reader_claims_nothing(self):
        now = time.time()
        ident, _ = timers.schedule(self.manifest, 'lead-a', 'live only', after='1m', now=now)
        self.manifest.mode = 'shadow'
        self.assertEqual(timers.fire(self.manifest, now=now + 61), [])
        self.assertEqual([r['id'] for _, r in timers.pending(self.manifest)], [ident])

    def test_refusals_limits_list_and_cancel(self):
        now = time.time()
        for kwargs, error in ((dict(after='10m', lane='nobody'), 'lane_not_registered'),
                              (dict(after='8d'), 'invalid_timer_time'),
                              (dict(at='2030-01-02T03:04:00'), 'needs a zone'),
                              (dict(at=timers.stamp(now - 5)), 'invalid_timer_time'),
                              (dict(), 'timer_needs_one_of'),
                              (dict(after='1m', at=timers.stamp(now + 60)), 'timer_needs_one_of'),
                              (dict(after='1m', text=''), 'invalid_timer_text'),
                              (dict(after='1m', text='x' * 5000), 'invalid_timer_text'),
                              (dict(after='1m', text='x' * (self.manifest.bus.text_cap - 50)), 'invalid_timer_text'),
                              (dict(after='1m', frm='bad name'), 'invalid_timer_sender')):
            lane, text = kwargs.pop('lane', 'lead-a'), kwargs.pop('text', 'hello')
            with self.subTest(error=error), self.assertRaisesRegex(ManifestError, error):
                timers.schedule(self.manifest, lane, text, now=now, **kwargs)
        ident, _ = timers.schedule(self.manifest, 'lead-a', 'one', after='5m', now=now)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(['--manifest', str(self.file), 'wake-at', '--list']), 0)
            self.assertEqual(main(['--manifest', str(self.file), 'wake-at', '--cancel', ident]), 0)
            self.assertEqual(main(['--manifest', str(self.file), 'wake-at', '--cancel', ident]), 0)
            self.assertEqual(main(['--manifest', str(self.file), 'wake-at', 'lead-a', 'from the cli', '--in', '2h']), 0)
        text = out.getvalue()
        self.assertIn(f'TIMER {ident} ', text)
        self.assertIn('TIMER_CANCELLED ' + ident, text)
        self.assertIn('TIMER_ABSENT ' + ident, text)
        self.assertRegex(text, r'TIMER_SET [0-9a-f]{12} \S+Z to=lead-a')
        with patch.object(timers, 'MAX_PER_LANE', 1), self.assertRaisesRegex(ManifestError, 'timer_limit'):
            timers.schedule(self.manifest, 'lead-a', 'two', after='5m', now=now)

    def test_the_wake_scan_fires_due_timers_and_then_wakes_for_them(self):
        now = time.time()
        wake.scan(self.manifest)  # first scan starts at the end of the log
        ident, _ = timers.schedule(self.manifest, 'lead-a', 'scan me', after='1m', now=now - 120)
        seen = []
        out = io.StringIO()
        with patch.object(wake, 'notify', side_effect=lambda manifest, rows: seen.extend(rows)), \
                contextlib.redirect_stdout(out):
            wake.scan(self.manifest)
        self.assertIn('WAKE_TIMERS fired=' + ident, out.getvalue())
        self.assertIn('wake-at-' + ident, [row.get('id') for row in seen])

    def test_a_timer_for_a_lane_retired_meanwhile_is_dropped_without_a_row(self):
        now = time.time()
        timers.schedule(self.manifest, 'lead-a', 'too late', after='1m', now=now)
        with contextlib.redirect_stdout(io.StringIO()):
            wake.change(self.manifest, 'lead-a', 'coordinator')
        self.assertEqual(timers.fire(self.manifest, now=now + 61), [])
        self.assertEqual(self.rows(), [])
        self.assertEqual(timers.pending(self.manifest), [])
