"""Pool values may be agentctl selectors; OpenCode lanes need no Claude resume adapter."""
import contextlib
import io
import time
from pathlib import Path
from unittest.mock import patch

from coordkit import lane
from coordkit.manifest import ManifestError, load, parse_selector
from coordkit.process import ToolError
from coordkit.state import write_json
from tests.support import FakeCase


class SelectorManifestTests(FakeCase):
    def setUp(self):
        super().setUp()
        self.base = self.file.read_text()

    def load_with(self, value):
        self.file.write_text(self.base.replace('implement = "pool:implement"', f'implement = "{value}"'))
        return load(self.file)

    def test_selector_values_are_passed_through_unchanged(self):
        self.assertEqual(parse_selector('family:kimi'), {'family': 'kimi'})
        self.assertEqual(parse_selector('harness:opencode+model:morph/morph-kimik31'),
                         {'harness': 'opencode', 'model': 'morph/morph-kimik31'})
        for value in ('family:kimi', 'family:deepseek', 'model:opencode/morph-kimik31',
                      'harness:opencode+model:morph/morph-kimik31'):
            with self.subTest(value=value):
                self.assertEqual(self.load_with(value).agentctl.pools['implement'], value)

    def test_malformed_selectors_refuse_at_load(self):
        for value in ('family:', 'bogus:kimi', 'family:kimi+family:x', 'pool:a+family:b',
                      'family:kimi ', 'family: ki'):
            with self.subTest(value=value), self.assertRaises(ManifestError):
                self.load_with(value)


class LaneLaunchTests(FakeCase):
    def plan(self, **overrides):
        path = self.manifest.paths.out_dir/'req/worker.plan.json'
        plan = dict(label='worker', key='ck-sample-worker', started=0, plan_file=str(path),
                    execution_id='', phase='launching', prompt='Complete safely.', pool='implement',
                    owns=[], access='read_only', bus_start=0)
        plan.update(overrides)
        return plan

    def delegate_request(self):
        return next(row['request'] for row in self.calls()
                    if row['tool'] == 'agentctl' and 'delegate' in row['args'])

    def test_family_selector_launches_without_claude_state(self):
        self.manifest.agentctl.pools['implement'] = 'family:deepseek'
        plan = self.plan()
        lane.launch_run(self.manifest, plan)
        self.assertEqual(self.delegate_request()['selector'],
                         {'family': 'deepseek', 'settings': {'access': 'read_only'}})
        self.assertEqual(plan['execution_id'], 'exec-fake-one-two-three-four-five')
        # No manifest lane, session_id or resume recipe is required to launch.
        self.assertEqual(self.manifest.lanes, [])

    def test_plan_selector_dict_wins_over_the_pool_name(self):
        plan = self.plan(selector={'family': 'kimi'})
        lane.launch_run(self.manifest, plan)
        self.assertEqual(self.delegate_request()['selector']['family'], 'kimi')

    def test_raw_selector_in_the_plan_pool_field(self):
        plan = self.plan(pool='family:kimi')
        lane.launch_run(self.manifest, plan)
        self.assertEqual(self.delegate_request()['selector']['family'], 'kimi')

    def test_opencode_model_launches_with_coding_access(self):
        plan = self.plan(pool='implement', access='coding',
                         selector={'harness': 'opencode', 'model': 'morph/morph-kimik31'})
        lane.launch_run(self.manifest, plan)
        selector = self.delegate_request()['selector']
        self.assertEqual(selector['harness'], 'opencode')
        self.assertEqual(selector['settings'], {'access': 'coding'})

    def test_opencode_read_only_refuses_before_launch(self):
        plan = self.plan(selector={'harness': 'opencode', 'model': 'morph/morph-kimik31'})
        with self.assertRaises(ToolError) as caught:
            lane.launch_run(self.manifest, plan)
        self.assertEqual(caught.exception.category, 'plan_refused')
        self.assertFalse(any('delegate' in row['args'] for row in self.calls()))

    def test_capacity_fallback_replaces_a_pinned_selector(self):
        plan = self.plan(selector={'family': 'kimi'}, execution_id='exec-old', phase='running',
                         started=time.time(), autocontinued=True)
        write_json(Path(plan['plan_file']), plan)
        self.manifest.lane.capacity_fallback = ['plan']
        capacity = {'state': 'cancelled', 'failure': {'code': 'provider_capacity'}}
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                patch.object(lane.AgentctlClient, 'await_result',
                             side_effect=[ToolError('agentctl', 10, 'capacity'), 'done']), \
                patch.object(lane.AgentctlClient, 'status', return_value=capacity):
            self.assertEqual(lane.watch(self.manifest, plan), 0)
        self.assertEqual(self.delegate_request()['selector'],
                         {'pool': 'plan', 'settings': {'access': 'read_only'}})

    def test_relaunch_on_the_original_pool_keeps_the_pinned_selector(self):
        plan = self.plan(selector={'family': 'kimi'})
        lane.launch_run(self.manifest, plan, '-r1', pool='implement')
        self.assertEqual(self.delegate_request()['selector']['family'], 'kimi')
