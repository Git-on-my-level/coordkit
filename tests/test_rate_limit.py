"""Provider 429 retries are rate limiting, not a hung lane."""
import contextlib
import datetime as dt
import io
import time
from pathlib import Path
from unittest.mock import patch

from coordkit import lane
from coordkit.process import ToolError
from coordkit.state import read_json, write_json
from tests.support import FakeCase


def stamp(age):
    return dt.datetime.fromtimestamp(time.time() - age, dt.timezone.utc).isoformat()


class RateLimitShapeTests(FakeCase):
    def test_tolerant_status_shapes(self):
        self.assertEqual(lane.rate_limit({'state': 'rate_limited', 'retry_after_ms': 1200}),
                         {'retry_after_ms': 1200, 'reset_at': None, 'model': None, 'status': 429})
        self.assertEqual(lane.rate_limit({'rate_limit': {'retry_after_ms': 5}})['retry_after_ms'], 5)
        self.assertEqual(lane.rate_limit({'failure': {'code': 'provider_rate_limit'}})['status'], 429)
        self.assertEqual(lane.rate_limit({'error': {'status': 429, 'model': 'morph-kimik31'}})['model'],
                         'morph-kimik31')
        self.assertEqual(lane.rate_limit({'events': [
            {'type': 'retry', 'error': {'type': 'provider.rate-limit', 'status': 429, 'retryAfterMs': 900}}]}
        )['retry_after_ms'], 900)
        # Unrelated states, capacity failures and body text never look like throttling.
        for status in (None, [], {'state': 'running', 'liveness': 'unreachable'},
                       {'failure': {'code': 'provider_capacity'}},
                       {'events': [{'type': 'retry', 'error': {'message': '429 rate limit'}}]}):
            self.assertIsNone(lane.rate_limit(status), status)

    def test_evidence_must_be_fresh_and_carry_a_structured_marker(self):
        now = time.time()
        fresh = {'state': 'running', 'events': [
            {'type': 'retry', 'at': stamp(30), 'error': {'status': 429, 'retryAfterMs': 1000}}]}
        self.assertIsNotNone(lane.rate_limit(fresh, now))
        stale = {'state': 'running', 'updated_at': stamp(3 * 3600), 'events': [
            {'type': 'retry', 'at': stamp(3 * 3600), 'error': {'status': 429, 'retryAfterMs': 1000}}]}
        self.assertIsNone(lane.rate_limit(stale, now))
        # A generic retry object is not evidence unless it carries the 429 marker.
        generic = {'state': 'running', 'updated_at': stamp(30), 'retry': {'retry_after_ms': 1000}}
        self.assertIsNone(lane.rate_limit(generic, now))
        marked = dict(generic, retry={'status': 429, 'retry_after_ms': 1000})
        self.assertEqual(lane.rate_limit(marked, now)['retry_after_ms'], 1000)

    def test_non_finite_and_negative_retry_values_are_ignored_not_trusted(self):
        now = time.time()
        for value in (float('inf'), float('-inf'), float('nan'), -1, -1000, 10 ** 400):
            with self.subTest(value=value):
                evidence = lane.rate_limit(
                    {'state': 'rate_limited', 'retry_after_ms': value}, now)
                # The 429 marker is still evidence, but the delay itself is unusable.
                self.assertIsNotNone(evidence)
                self.assertIsNone(evidence['retry_after_ms'])
                # An unusable delay must not keep stale evidence alive.
                self.assertIsNone(lane.rate_limit(
                    {'state': 'rate_limited', 'retry_after_ms': value,
                     'updated_at': stamp(3 * 3600)}, now))

    def test_huge_retry_delays_are_capped_instead_of_pinning_a_lane(self):
        now = time.time()
        huge = {'state': 'rate_limited', 'retry_after_ms': 10 ** 18,
                'updated_at': stamp(3 * 3600)}
        self.assertIsNone(lane.rate_limit(huge, now))
        fresh = dict(huge, updated_at=stamp(30))
        self.assertEqual(lane.rate_limit(fresh, now)['retry_after_ms'],
                         lane.MAX_RETRY_AFTER_MS)

    def test_non_finite_evidence_timestamps_are_discarded(self):
        for value in (float('inf'), float('-inf'), float('nan'), '1e309', '-1e309', 'nan',
                      -1, 10 ** 400):
            with self.subTest(value=value):
                self.assertIsNone(lane._epoch(value))

    def test_future_reset_deadline_extends_freshness_only_to_the_cap(self):
        now = time.time()
        recent = {'failure': {'status': 429, 'occurred_at': stamp(10 * 60),
                              'reset_at': now + 30 * 60}}
        self.assertIsNotNone(lane.rate_limit(recent, now))
        # A reset far beyond the cap cannot make hours-old evidence current.
        far = {'failure': {'status': 429, 'occurred_at': stamp(3 * 3600),
                           'reset_at': now + 10 * 365 * 86400}}
        self.assertIsNone(lane.rate_limit(far, now))
        # A past reset does not extend the base evidence window.
        past = {'failure': {'status': 429, 'occurred_at': stamp(3 * 3600),
                            'reset_at': stamp(3600)}}
        self.assertIsNone(lane.rate_limit(past, now))

    def test_provider_rate_limit_event_without_a_numeric_status(self):
        now = time.time()
        status = {'events': [{'type': 'retry', 'at': stamp(30),
                              'error': {'type': 'provider.rate_limit'}}]}
        self.assertIsNotNone(lane.rate_limit(status, now))


class RateLimitRecoveryTests(FakeCase):
    def quiet(self, fn, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return fn(*args, **kwargs)

    def plan(self):
        path = self.manifest.paths.out_dir/'req/worker.plan.json'
        plan = dict(label='worker', key='ck-sample-worker', started=time.time(), plan_file=str(path),
                    execution_id='exec-throttled', phase='running', prompt='complete', pool='implement',
                    card='card:task', owns=[], access='read_only', bus_start=0, autocontinued=True)
        write_json(path, plan)
        return plan

    def test_recovery_renews_a_rate_limited_run_instead_of_expiring_it(self):
        plan = self.plan()
        throttled = {'state': 'running', 'liveness': 'unreachable', 'updated_at': stamp(30),
                     'rate_limit': {'retry_after_ms': 2500, 'model': 'morph-kimik31'}}
        with patch.object(lane.AgentctlClient, 'status', return_value=throttled), \
                patch.object(lane, 'detach_watcher') as restored:
            self.assertTrue(self.quiet(lane.recover, self.manifest, plan))
        restored.assert_called_once()
        saved = read_json(__import__('pathlib').Path(plan['plan_file']))
        self.assertNotIn('terminal', saved)
        self.assertEqual(saved['rate_limit']['retry_after_ms'], 2500)
        self.assertFalse(any('cancel' in row['args'] or 'release' in row['args'] for row in self.calls()))
        self.assertTrue(any('renew' in row['args'] for row in self.calls()))
        self.assertFalse(any('ask' in row['args'] for row in self.calls()))
        events = [__import__('json').loads(line) for line in self.manifest.paths.events.read_text().splitlines()]
        self.assertEqual([row['kind'] for row in events if row['kind'] == 'rate_limited'], ['rate_limited'])

    def test_recovery_reacquires_a_lapsed_lease_before_restoring_the_watcher(self):
        plan = self.plan()
        # The watcher died (unreachable) while the run was throttled; the evidence
        # itself is fresh, so recovery must keep the run and reacquire the lease.
        throttled = {'state': 'running', 'liveness': 'unreachable', 'updated_at': stamp(30),
                     'rate_limit': {'retry_after_ms': 2500}}
        self.respond({'tool': 'anx', 'contains': ['renew'], 'exit': 13,
                      'json': {'ok': False, 'error': {'code': 'lease_held'}}},
                     {'tool': 'anx', 'contains': ['show'],
                      'json': {'ok': True, 'result': {'lease': {'owner': 'worker'}}}})
        with patch.object(lane.AgentctlClient, 'status', return_value=throttled), \
                patch.object(lane, 'detach_watcher') as restored:
            self.assertTrue(self.quiet(lane.recover, self.manifest, plan))
        restored.assert_called_once()
        self.assertTrue(any('renew' in row['args'] for row in self.calls()))
        self.assertTrue(any('acquire' in row['args'] for row in self.calls()))
        self.assertFalse(any('ask' in row['args'] or 'release' in row['args'] for row in self.calls()))
        self.assertNotIn('terminal', read_json(Path(plan['plan_file'])))

    def test_rate_limited_recovery_leaves_another_owners_lease(self):
        plan = self.plan()
        throttled = {'state': 'running', 'rate_limit': {'retry_after_ms': 2500}}
        self.respond({'tool': 'anx', 'contains': ['renew'], 'exit': 2,
                      'json': {'ok': False, 'error': {'code': 'not_found'}}},
                     {'tool': 'anx', 'contains': ['show'],
                      'json': {'ok': True, 'result': {'lease': {'owner': 'actor:other'}}}})
        with patch.object(lane.AgentctlClient, 'status', return_value=throttled), \
                patch.object(lane, 'detach_watcher') as restored:
            self.assertTrue(self.quiet(lane.recover, self.manifest, plan))
        restored.assert_not_called()
        self.assertFalse(any('acquire' in row['args'] for row in self.calls()))
        asks = [row for row in self.calls() if 'ask' in row['args']]
        self.assertTrue(asks and any('lease_owner_changed' in ' '.join(row['args']) for row in asks))

    def test_watcher_records_throttling_then_completes(self):
        plan = self.plan()
        throttled = {'state': 'running', 'liveness': 'unreachable', 'updated_at': stamp(30),
                     'rate_limit': {'retry_after_ms': 1000}}
        with patch.object(lane.AgentctlClient, 'await_result', side_effect=[ToolError('agentctl', 9, 'await timeout'), 'answer']), \
                patch.object(lane.AgentctlClient, 'status', return_value=throttled):
            self.assertEqual(self.quiet(lane.watch, self.manifest, plan), 0)
        saved = read_json(__import__('pathlib').Path(plan['plan_file']))
        self.assertEqual((saved['terminal'], saved['completion']['rc']), ('completed', 0))
        self.assertEqual(saved['rate_limit']['retry_after_ms'], 1000)
        self.assertFalse(any('cancel' in row['args'] for row in self.calls()))

    def test_stale_429_falls_back_to_the_normal_hung_check(self):
        plan = self.plan()
        stale = {'state': 'running', 'liveness': 'unreachable', 'updated_at': stamp(3 * 3600),
                 'events': [{'type': 'retry', 'at': stamp(3 * 3600),
                             'error': {'status': 429, 'retryAfterMs': 1000}}]}
        with patch.object(lane.AgentctlClient, 'status',
                          side_effect=[stale, stale, stale, {'state': 'cancelled'}]):
            self.quiet(lane.recover, self.manifest, plan)
        self.assertTrue(any('cancel' in row['args'] for row in self.calls()))
        self.assertTrue(any('ask' in row['args'] and 'execution_hung' in ' '.join(row['args'])
                            for row in self.calls()))
        events = [__import__('json').loads(line) for line in self.manifest.paths.events.read_text().splitlines()]
        self.assertFalse(any(row['kind'] == 'rate_limited' for row in events))

    def test_lanes_view_labels_a_throttled_execution(self):
        from coordkit import mcp_server
        rows = {'executions': [{'id': 'exec-throttled', 'state': 'running', 'labels': ['worker'],
                                'rate_limit': {'retry_after_ms': 2500}}], 'has_more': False}
        with patch.object(mcp_server.shutil, 'which', return_value='/fake/agentctl'), \
                patch('coordkit.process.call_json', return_value=rows):
            result = mcp_server.lane_rows(self.manifest, {'limit': 10})
        row = next(item for item in result['rows'] if item['id'] == 'exec-throttled')
        self.assertEqual(row['state'], 'rate_limited')
        self.assertEqual(row['rate_limited']['retry_after_ms'], 2500)
