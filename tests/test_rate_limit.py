"""Provider 429 retries are rate limiting, not a hung lane."""
import contextlib
import datetime as dt
import io
import time
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
        throttled = {'state': 'running', 'liveness': 'unreachable', 'updated_at': stamp(3 * 3600),
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

    def test_watcher_records_throttling_then_completes(self):
        plan = self.plan()
        throttled = {'state': 'running', 'liveness': 'unreachable', 'updated_at': stamp(3 * 3600),
                     'rate_limit': {'retry_after_ms': 1000}}
        with patch.object(lane.AgentctlClient, 'await_result', side_effect=[ToolError('agentctl', 9, 'await timeout'), 'answer']), \
                patch.object(lane.AgentctlClient, 'status', return_value=throttled):
            self.assertEqual(self.quiet(lane.watch, self.manifest, plan), 0)
        saved = read_json(__import__('pathlib').Path(plan['plan_file']))
        self.assertEqual((saved['terminal'], saved['completion']['rc']), ('completed', 0))
        self.assertEqual(saved['rate_limit']['retry_after_ms'], 1000)
        self.assertFalse(any('cancel' in row['args'] for row in self.calls()))

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
