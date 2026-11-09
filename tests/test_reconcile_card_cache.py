"""An obligation receipt must prevent repeat I/O, including missing-card I/O."""
import contextlib
import dataclasses
import fcntl
import io
import json
from pathlib import Path
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

    def test_repaired_card_hourly_recheck_includes_handled_rows(self):
        self.rows(3)
        self.missing()
        self.run_pass()
        cache_key = reconcile.missing_card_key(self.manifest, 'card:missing-0')
        self.assertEqual(self.state()['missing_cards'][cache_key]['expires_at'], 31600)
        receipts = self.state()['fired']
        self.respond()  # card now exists, without closing or replacing any row
        self.run_pass(at=13599)
        self.assertEqual(len(self.lookups()), 1)
        self.run_pass(at=13600)
        self.assertEqual(len(self.lookups()), 2)
        self.assertEqual(self.state()['missing_cards'], {})
        self.assertEqual(self.state()['unresolved_obligations'], {})
        self.assertEqual(self.state()['fired'], receipts)  # never send the asks again
        self.run_pass(at=17200)
        self.assertEqual(len(self.lookups()), 2)

    def test_repeated_misses_reverify_once_per_hour_per_ref(self):
        self.rows(6, 2)
        self.missing()
        self.run_pass()
        self.run_pass(at=13599)
        self.assertEqual(len(self.lookups()), 2)
        self.run_pass(at=13600)
        self.assertEqual(len(self.lookups()), 4)
        self.rows(6, 2, prefix='fresh')
        self.run_pass(at=13601)
        self.assertEqual(len(self.lookups()), 4)
        self.run_pass(at=17200)
        self.assertEqual(len(self.lookups()), 6)

    def test_workspace_identity_switch_invalidates_cache_and_recovers_handled_row(self):
        self.rows()
        self.missing()
        self.run_pass()
        initial = self.state()
        original = self.manifest.anx
        for field, value in [('workspace', 'different'), ('lead_actor', 'other-actor'),
                             ('base_url', 'https://other.example')]:
            with self.subTest(field=field):
                write_json(self.manifest.paths.root/'reconcile/state.json', initial)
                self.manifest.anx = dataclasses.replace(original, **{field: value})
                self.respond()  # same card exists in the new identity
                before = len(self.lookups())
                self.run_pass(at=10300)  # before the hourly recheck
                self.assertEqual(len(self.lookups()), before+1)
                self.assertEqual(self.state()['missing_cards'], {})
                self.assertEqual(self.state()['unresolved_obligations'], {})
                self.assertEqual(self.state()['fired'], initial['fired'])

    def test_negative_cache_cap_evicts_oldest_on_load_and_insert(self):
        scope = reconcile.card_cache_scope(self.manifest)
        entries = {reconcile.missing_card_key(self.manifest, f'card:old-{i}'):
                   dict(scope=scope, ref=f'card:old-{i}', created_at=i,
                        checked_at=9000, expires_at=50000) for i in range(2001)}
        self.rows(0)
        path = self.manifest.paths.root/'reconcile/state.json'
        write_json(path, dict(fired={}, dead={}, loops={}, missing_cards=entries))
        self.run_pass()
        self.assertEqual(len(self.state()['missing_cards']), 2000)
        self.assertNotIn(reconcile.missing_card_key(self.manifest, 'card:old-0'), self.state()['missing_cards'])
        self.rows()
        self.missing()
        self.run_pass(at=10300)
        current = self.state()['missing_cards']
        self.assertEqual(len(current), 2000)
        self.assertNotIn(reconcile.missing_card_key(self.manifest, 'card:old-1'), current)
        self.assertIn(reconcile.missing_card_key(self.manifest, 'card:missing-0'), current)

    def test_expired_and_legacy_unscoped_misses_are_dropped(self):
        self.rows()
        scope = reconcile.card_cache_scope(self.manifest)
        missing = {'card:missing-0': dict(expires_at=50000),
                   reconcile.missing_card_key(self.manifest, 'card:expired'):
                   dict(scope=scope, ref='card:expired', created_at=0, checked_at=0, expires_at=10000)}
        write_json(self.manifest.paths.root/'reconcile/state.json',
                   dict(fired={}, dead={}, loops={}, missing_cards=missing))
        self.run_pass()
        self.assertEqual(len(self.lookups()), 1)
        self.assertEqual(self.state()['missing_cards'], {})

    def test_forget_one_and_all_recheck_handled_rows_without_network_or_reasking(self):
        self.rows(2, 2)
        self.missing()
        self.run_pass()
        fired = self.state()['fired']
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(reconcile.main(['forget-missing', 'card:missing-0'], self.manifest), 0)
        self.assertEqual(len(self.lookups()), 2)  # operator command is entirely local
        self.assertEqual(len(self.state()['missing_cards']), 1)
        self.respond()
        self.run_pass(at=10300)
        self.assertEqual(len(self.lookups()), 3)
        self.assertEqual(set(self.state()['unresolved_obligations']), {'ob-1'})
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(reconcile.main(['forget-missing', '--all'], self.manifest), 0)
        self.assertEqual(self.state()['missing_cards'], {})
        self.run_pass(at=10600)
        self.assertEqual(len(self.lookups()), 4)
        self.assertEqual(self.state()['unresolved_obligations'], {})
        self.assertEqual(self.state()['fired'], fired)

    def test_forget_requires_one_selector_and_respects_reconcile_lock(self):
        self.rows()
        self.missing()
        self.run_pass()
        path = self.manifest.paths.root/'reconcile/state.json'
        before = path.read_bytes()
        for args in [[], ['--all', 'card:x'], ['--all', '--dry-run']]:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                try:
                    result = reconcile.main(['forget-missing', *args], self.manifest)
                except SystemExit as exc:
                    result = exc.code
                self.assertIn(result, (2, 6))
            self.assertEqual(path.read_bytes(), before)
        with (path.parent/'run.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(reconcile.main(['forget-missing', '--all'], self.manifest), 6)
        self.assertEqual(path.read_bytes(), before)

    def test_failed_wake_does_not_increment_blocker_counter_or_ask_early(self):
        self.rows(2, 2, kind='blocker')
        self.missing()
        self.manifest.loop.fail_report_after = 2
        with contextlib.redirect_stdout(io.StringIO()), patch.object(reconcile.escalation, 'ask'), \
                patch.object(bus, 'main', side_effect=ToolError('bus', 6, 'wake failed')), \
                self.assertRaises(ToolError):
            reconcile.reconcile(self.manifest, at=10000)
        self.assertNotIn('ob-1', self.state()['blockers'])
        self.assertNotIn('wake:blocker:ob-1', self.state()['fired'])
        self.run_pass(at=10300)
        self.assertEqual(self.state()['blockers']['ob-1'], 1)
        self.assertIn('wake:blocker:ob-1', self.state()['fired'])
        self.assertNotIn('blocker:ob-1', self.state()['fired'])
        self.run_pass(at=10600)
        self.assertEqual(self.state()['blockers']['ob-1'], 2)
        self.assertIn('blocker:ob-1', self.state()['fired'])

    def test_delivered_wake_counter_survives_later_failure(self):
        self.rows(2, 2, kind='blocker')
        self.manifest.loop.fail_report_after = 2
        with patch.object(reconcile, 'report', side_effect=RuntimeError('later failure')), \
                self.assertRaises(RuntimeError):
            self.run_pass()
        self.assertIn('wake:blocker:ob-1', self.state()['fired'])
        self.assertEqual(self.state()['blockers']['ob-1'], 1)
        self.run_pass(at=10300)
        self.assertEqual(self.state()['blockers']['ob-1'], 2)
        self.assertIn('blocker:ob-1', self.state()['fired'])

    def test_bad_handle_warning_does_not_block_apply_but_outage_still_red(self):
        self.rows()
        with patch.object(reconcile, 'card_for', side_effect=reconcile.CardUnresolved('bad handle', 'invalid handle')):
            self.run_pass()
        self.manifest.launchd.path = [str(Path.home()/'.local/bin'), str(self.root/'fakes'), '/usr/bin', '/bin']
        self.respond({'tool':'anx','contains':['orient'],'json':{'ok':True,'result':{'principal':{'id':'lead'}}}},
                     {'tool':'anx','contains':['boards','get'],'json':{'ok':True,'result':{'board':{'id':'board'}}}},
                     {'tool':'agentctl','contains':['config','doctor'],'json':{'ok':True,'result':{'valid':True}}})
        # No job installation: exercise the real apply health gate with an empty
        # synthetic job plan and mocked registration, without a service mutation.
        with patch.object(lifecycle, 'templates', return_value=[]), \
                patch('coordkit.selfupdate.register'), patch.object(lifecycle.time, 'time', return_value=10001), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            try:
                result = lifecycle.up(self.manifest, apply=True)
            except lifecycle.ManifestError:
                self.fail(out.getvalue())
            self.assertEqual(result, 0)
        self.assertIn('DOCTOR unresolved_obligations warn', out.getvalue())
        self.respond({'tool':'anx','contains':['orient'],'exit':6,
                      'json':{'ok':False,'error':{'code':'unavailable'}}})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(lifecycle.doctor(self.manifest), 1)
        self.assertIn('DOCTOR anx red', out.getvalue())

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

    def test_handled_transport_and_auth_failures_retry_next_pass_without_hour_delay(self):
        for code, exit_code in [('unavailable', 6), ('forbidden', 3)]:
            with self.subTest(code=code):
                self.rows(3, prefix=code)
                self.missing(code, exit_code)
                self.run_pass()
                self.assertEqual(self.state()['missing_cards'], {})
                self.assertTrue(all('checked_at' not in row for row in self.state()['unresolved_obligations'].values()))
                self.respond()
                before = len(self.lookups())
                self.run_pass(at=10300)
                self.assertEqual(len(self.lookups()), before+1)
                self.assertEqual(self.state()['unresolved_obligations'], {})

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

    def test_renewed_miss_survives_failed_ask_and_later_stage_failure(self):
        self.rows()
        self.missing()
        error = ToolError('bus', 6, 'ask unavailable')
        self.run_pass(ask_error=error)
        with patch.object(reconcile, 'report', side_effect=RuntimeError('later failure')), \
                self.assertRaises(RuntimeError):
            self.run_pass(at=13600, ask_error=error)
        key = reconcile.missing_card_key(self.manifest, 'card:missing-0')
        self.assertEqual(self.state()['missing_cards'][key]['checked_at'], 13600)
        self.assertEqual(self.state()['missing_cards'][key]['expires_at'], 35200)
        self.run_pass(at=13601, ask_error=error)
        self.assertEqual(len(self.lookups()), 2)

    def test_due_reverification_dry_run_preserves_cache_and_unresolved_receipts(self):
        self.rows()
        self.missing()
        self.run_pass()
        path = self.manifest.paths.root/'reconcile/state.json'
        before = path.read_bytes()
        self.respond()
        self.run_pass(at=13600, dry=True)
        self.assertEqual(len(self.lookups()), 2)
        self.assertEqual(path.read_bytes(), before)

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

    def _obligation(self, kind, ident):
        path = self.manifest.paths.bus_log
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(id=ident, kind=kind, owner='worker', frm='worker', to='lead',
                                        origin='coordkit', subject='release-train', text='needs action',
                                        ts='1970-01-01T00:00:00Z'))+'\n')
        self.manifest.bus.mirror = False
        state = self.manifest.paths.root/'reconcile/state.json'
        if state.exists():
            state.unlink()

    def _passes(self, *ats, asks=None):
        # Append before returning so a later failure in the pass still leaves the count.
        if asks is None:
            asks = []
        for at in ats:
            with contextlib.redirect_stdout(io.StringIO()), patch.object(reconcile.escalation, 'ask',
                    side_effect=lambda *args, **kwargs: asks.append(args)), patch.object(bus, 'main', return_value=0):
                reconcile.reconcile(self.manifest, at=at)
        return asks

    def _doctor_line(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            lifecycle.doctor(self.manifest)
        return next(line for line in out.getvalue().splitlines() if 'DOCTOR unresolved_obligations' in line)

    def test_ambiguous_mapping_clears_after_linkage_is_repaired_without_reasking(self):
        # Two execution cards, then one. The handled ask must not stick card=None.
        self._obligation('ruling', 'map-1')
        linked = lambda *cards: {'tool': 'agentctl', 'contains': ['recent'], 'json': {'ok': True, 'result': {
            'executions': [{'id': f'e{i}', 'labels': ['worker', f'anx.card.{card}'], 'state': 'running'}
                           for i, card in enumerate(cards)]}}}
        self.respond(linked('alpha', 'beta'))
        asks = self._passes(10000, 10300)
        record = self.state()['unresolved_obligations']['map-1']
        self.assertIsNone(record['card'])
        self.assertIn('ambiguous', record['detail'])
        self.assertIn('ruling:map-1', self.state()['fired'])
        self.assertEqual(len(asks), 1)
        self.assertIn(' warn ', self._doctor_line())
        self.respond(linked('alpha'))
        before = len(self.lookups())
        with patch.object(reconcile, 'report', side_effect=RuntimeError('later failure')), self.assertRaises(RuntimeError):
            self._passes(10600, asks=asks)
        self.assertEqual(len(self.lookups()), before+1)
        self.assertIn('card:alpha', self.lookups()[-1]['args'])
        self.assertEqual(self.state()['unresolved_obligations'], {})
        self.assertEqual(len(asks), 1)
        self.assertIn(' green ', self._doctor_line())

    def test_agentctl_mapping_error_clears_after_linkage_returns_without_reasking(self):
        self.manifest.loop.fail_report_after = 1
        self._obligation('blocker', 'map-1')
        self.respond({'tool': 'agentctl', 'contains': ['--label'], 'exit': 6,
                      'json': {'ok': False, 'error': {'code': 'unavailable'}}})
        asks = self._passes(10000, 10300)
        record = self.state()['unresolved_obligations']['map-1']
        self.assertIsNone(record['card'])
        self.assertIn('unavailable', record['detail'])
        self.assertIn('blocker:map-1', self.state()['fired'])
        self.assertIn('wake:blocker:map-1', self.state()['fired'])
        self.assertEqual(len(asks), 1)
        self.assertEqual(len(self.lookups()), 0)
        self.respond({'tool': 'agentctl', 'contains': ['--label'], 'json': {'ok': True, 'result': {'executions': [
            {'id': 'e1', 'labels': ['worker', 'anx.card.alpha'], 'state': 'running'}]}}})
        with patch.object(reconcile, 'report', side_effect=RuntimeError('later failure')), self.assertRaises(RuntimeError):
            self._passes(10600, asks=asks)
        self.assertEqual(self.state()['unresolved_obligations'], {})
        self.assertEqual(len(asks), 1)
        self.assertIn(' green ', self._doctor_line())

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
