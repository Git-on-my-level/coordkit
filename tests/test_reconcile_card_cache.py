"""An obligation receipt must prevent repeat I/O, including missing-card I/O."""
import contextlib
import dataclasses
import io
import json
import time
from unittest.mock import patch

from coordkit import bus, lifecycle, reconcile
from coordkit.process import ToolError
from coordkit.state import read_json, write_json
from tests.support import FakeCase


class ReconcileCardCacheTests(FakeCase):
    def rows(self, count=1, distinct=1, prefix='ob', kind='ruling'):
        rows = [dict(id=f'{prefix}-{i}', kind=kind if i % 2 else 'ruling',
                     owner=f'owner-{i}', frm='worker', to='lead', origin='coordkit',
                     subject=f'card:missing-{i % distinct}', text='needs action',
                     ts='1970-01-01T00:00:00Z') for i in range(count)]
        path = self.manifest.paths.bus_log
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        self.manifest.bus.mirror = False
        return rows

    def missing(self, code='not_found', exit_code=2):
        self.respond({'tool': 'anx', 'contains': ['cards', 'get'], 'exit': exit_code,
                      'json': {'ok': False, 'error': {'code': code}}})

    def run_pass(self, at=10000, dry=False, ask_error=None):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(reconcile.escalation, 'ask', side_effect=ask_error), \
                patch.object(bus, 'main', return_value=0):
            reconcile.reconcile(self.manifest, dry_run=dry, at=at)
        return output.getvalue()

    def lookups(self):
        return [c for c in self.calls() if c['tool'] == 'anx' and 'get' in c['args'] and 'cards' in c['args']]

    def state(self):
        return read_json(self.manifest.paths.root/'reconcile/state.json')

    def test_300_obligations_66_missing_cards_one_lookup_each(self):
        self.rows(300, 66, kind='blocker')
        self.manifest.loop.fail_report_after = 1
        self.missing()
        started = time.perf_counter()
        output = self.run_pass()
        elapsed = time.perf_counter()-started
        calls = self.lookups()
        self.assertEqual(len(calls), 66)
        self.assertEqual(len({c['args'][-1] for c in calls}), 66)
        self.assertEqual(len(self.state()['unresolved_obligations']), 300)
        self.assertNotIn('RECONCILE card unresolved', output)
        started = time.perf_counter()
        with patch.object(reconcile, 'card_for', side_effect=AssertionError('handled card resolved')):
            self.run_pass(at=10300)
        handled_elapsed = time.perf_counter()-started
        self.assertEqual(len(self.lookups()), 66)  # handled keys resolve nothing
        # New obligations still share the durable miss cache across invocations.
        self.rows(300, 66, prefix='new', kind='blocker')
        started = time.perf_counter()
        self.run_pass(at=10600)
        cached_elapsed = time.perf_counter()-started
        self.assertEqual(len(self.lookups()), 66)
        print(f'CARD_CACHE_MEASUREMENT obligations=300 missing_cards=66 lookups=66 '
              f'fake_elapsed_seconds={elapsed:.6f} lookup_latency_model_seconds={66*3.2:.1f} '
              f'handled_pass_seconds={handled_elapsed:.6f} negative_cache_pass_seconds={cached_elapsed:.6f} '
              'handled_pass_lookups=0 negative_cache_pass_lookups=0')

    def test_successes_are_shared_across_owners_in_one_pass(self):
        self.rows(12, 3)
        self.run_pass()
        self.assertEqual(len(self.lookups()), 3)
        self.run_pass(at=10300)
        self.assertEqual(len(self.lookups()), 3)

    def test_blocker_waits_for_threshold_without_resolving_again(self):
        self.rows(2, 2, kind='blocker')
        self.manifest.loop.fail_report_after = 3
        self.run_pass()
        self.assertEqual(len(self.lookups()), 2)
        self.run_pass(at=10300)
        self.assertEqual(len(self.lookups()), 2)
        self.run_pass(at=10600)
        self.assertEqual(len(self.lookups()), 3)  # only blocker now needs its ask
        self.run_pass(at=10900)
        self.assertEqual(len(self.lookups()), 3)

    def test_miss_ttl_retries_and_success_clears_unresolved_card(self):
        self.rows()
        self.missing()
        self.run_pass()
        self.assertEqual(self.state()['missing_cards']['card:missing-0']['expires_at'], 31600)
        self.rows(prefix='new')
        self.run_pass(at=31599)
        self.assertEqual(len(self.lookups()), 1)
        self.respond()  # card now exists
        self.run_pass(at=31600, dry=True)  # handled rows do not force resolution
        # New unfired row at the exact expiry boundary must be retried.
        self.rows(prefix='recovered')
        self.run_pass(at=31600)
        self.assertEqual(len(self.lookups()), 2)
        self.assertEqual(self.state()['missing_cards'], {})
        self.assertEqual(self.state()['unresolved_obligations'], {})

    def test_transient_and_auth_failures_never_get_durable_negative_cache(self):
        for code, exit_code in [('unavailable', 6), ('forbidden', 3)]:
            with self.subTest(code=code):
                self.rows(2, 1, prefix=code)
                self.missing(code, exit_code)
                self.run_pass()
                self.assertEqual(self.state()['missing_cards'], {})
        # A new obligation must retry on the next pass, rather than wait six hours.
        before = len(self.lookups())
        self.rows(prefix='retry')
        self.run_pass(at=10300)
        self.assertGreater(len(self.lookups()), before)

    def test_closed_obligations_clear_doctor_finding_without_lookups(self):
        self.rows(3)
        self.missing()
        self.run_pass()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            lifecycle.doctor(self.manifest)
        lines = [line for line in out.getvalue().splitlines() if 'DOCTOR unresolved_obligations' in line]
        self.assertEqual(len(lines), 1)
        self.assertIn('unresolved obligations=3 cards=1', lines[0])
        self.rows(0)
        self.run_pass(at=10300)
        self.assertEqual(self.state()['unresolved_obligations'], {})
        self.assertEqual(len(self.lookups()), 2)  # doctor also validates escalation card

    def test_dry_run_preserves_cache_receipts(self):
        self.rows()
        self.missing()
        self.run_pass()
        path = self.manifest.paths.root/'reconcile/state.json'
        before = path.read_bytes()
        self.rows(prefix='new')
        self.run_pass(at=10300, dry=True)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(self.lookups()), 1)

    def test_shadow_doctor_ignores_live_unresolved_receipts(self):
        self.rows()
        self.missing()
        self.run_pass()
        path = self.manifest.paths.root/'reconcile/state.json'
        before = path.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            lifecycle.doctor(dataclasses.replace(self.manifest, mode='shadow'))
        self.assertIn('DOCTOR unresolved_obligations green shadow: reconciler not scheduled', out.getvalue())
        self.assertEqual(path.read_bytes(), before)

    def test_closed_cleanup_is_durable_even_when_a_later_stage_fails(self):
        self.rows()
        self.missing()
        self.run_pass()
        self.rows(0)
        with patch.object(reconcile, 'report', side_effect=RuntimeError('later failure')), \
                self.assertRaises(RuntimeError):
            self.run_pass(at=10300)
        self.assertEqual(self.state()['unresolved_obligations'], {})

    def test_repaired_card_cleanup_survives_failed_ask_and_later_failure(self):
        self.rows()
        self.missing()
        self.run_pass()
        self.rows(prefix='retry')
        self.respond()  # now exists, after the negative TTL
        with patch.object(reconcile, 'report', side_effect=RuntimeError('later failure')), \
                self.assertRaises(RuntimeError):
            self.run_pass(at=31600, ask_error=ToolError('anx', 6, 'unavailable'))
        self.assertEqual(self.state()['unresolved_obligations'], {})

    def test_confirmed_miss_survives_a_later_pass_failure(self):
        self.rows()
        self.missing()
        with patch.object(reconcile, 'report', side_effect=RuntimeError('later failure')), \
                self.assertRaises(RuntimeError):
            self.run_pass()
        self.assertNotIn('last_completed', self.state())
        self.assertEqual(len(self.state()['missing_cards']), 1)
        self.rows(prefix='retry')
        self.run_pass(at=10300)
        self.assertEqual(len(self.lookups()), 1)

    def test_completion_uses_end_clock_and_failed_pass_keeps_previous_completion(self):
        clock = [0]
        real_report = reconcile.report
        def report(*args, **kwargs):
            result = real_report(*args, **kwargs)
            clock[0] = 1710
            return result
        with patch.object(reconcile.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(reconcile, 'report', side_effect=report):
            self.run_pass(at=10000)
        self.assertEqual(self.state()['started_at'], 10000)
        self.assertEqual(self.state()['last_completed'], 11710)
        with patch.object(reconcile.AgentctlClient, 'recent', side_effect=RuntimeError('failed')), \
                self.assertRaises(RuntimeError):
            self.run_pass(at=12000)
        self.assertEqual(self.state()['started_at'], 12000)
        self.assertEqual(self.state()['last_completed'], 11710)

    def test_doctor_uses_completion_and_configured_interval(self):
        self.manifest.reconciler.interval_seconds = 900
        path = self.manifest.paths.root/'reconcile/state.json'
        write_json(path, {'started_at': 1000, 'last_run': 1000, 'last_completed': 2710})
        for now, color in [(2711, 'green'), (4510, 'green'), (4511, 'red')]:
            with patch.object(lifecycle.time, 'time', return_value=now), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                lifecycle.doctor(self.manifest)
            self.assertIn(f'DOCTOR reconciler_age {color}', out.getvalue())
            self.assertIn('interval=900s deadline=1800s', out.getvalue())
