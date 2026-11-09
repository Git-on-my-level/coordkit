import contextlib
import io
import json
from unittest.mock import patch
from coordkit import bus, loop
from coordkit.manifest import ManifestError
from tests.support import FakeCase


class ReviewRegressions(FakeCase):
    def post(self, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return bus.main(['post', '--from', 'worker', '--to', 'lead', *args], self.manifest)

    def cycle(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return loop.run_cycle(self.manifest, 'audit')

    def test_post_append_mirror_failure_recovers_without_delegate_or_duplicate(self):
        self.respond({'tool':'anx','contains':['note'],'exit':2,'stderr':'mirror down'})
        self.assertEqual(self.cycle(), 1)
        self.assertEqual(len(self.manifest.paths.bus_log.read_text().splitlines()), 1)
        self.respond()
        self.assertEqual(self.cycle(), 0)
        self.assertEqual(len(self.manifest.paths.bus_log.read_text().splitlines()), 1)
        delegates=[c for c in self.calls() if 'delegate' in c['args'] and '--plan' not in c['args']]
        self.assertEqual(len(delegates), 1)
        self.assertTrue(any('--met' in c['args'] for c in self.calls()))

    def test_shadow_cursor_wait_refused_before_state_creation(self):
        self.manifest.mode='shadow'
        with self.assertRaisesRegex(ManifestError, 'shadow_mode_read_only'):
            bus.main(['wait','--to','worker','--timeout','0'],self.manifest)
        self.assertFalse(self.manifest.paths.root.exists())
        self.assertEqual(self.calls(), [])

    def test_repeated_long_fyi_dedupes_and_does_not_spill_again(self):
        for _ in range(2):
            self.post('--kind','fyi','long message '*100)
        self.assertEqual(len(self.manifest.paths.bus_log.read_text().splitlines()),1)
        self.assertEqual(len(list((self.manifest.paths.root/'state/refs').glob('*'))),1)

    def test_failed_escalation_retries_after_threshold(self):
        self.respond({'tool':'agentctl','contains':['--wait'],'exit':11,'stderr':'failed'})
        with patch.object(loop.AnxClient,'ask',side_effect=OSError('ANX temporarily down')):
            for _ in range(3):
                self.assertEqual(self.cycle(),1)
        self.assertEqual(self.escalations(),[])
        self.assertEqual(self.cycle(),1)
        self.assertEqual(self.cycle(),1)
        self.assertEqual(len([c for c in self.calls() if c['tool'] == 'anx' and 'ask' in c['args']]),1,
                         'retried once ANX is back, then suppressed until the reminder is due')

    def test_colliding_lane_names_have_independent_inbox_and_wait_cursors(self):
        bus.configure(self.manifest)
        for kind in ('inbox','wait'):
            bus.set_cursor('a/b',kind,1)
            self.assertIsNone(bus.get_cursor('a_b',kind,None))
            bus.set_cursor('a_b',kind,2)
            self.assertEqual(bus.get_cursor('a/b',kind,None),1)
            self.assertEqual(bus.get_cursor('a_b',kind,None),2)

    def test_pending_escalation_retries_same_payload_even_when_cycle_recovers(self):
        self.respond({'tool':'agentctl','contains':['--wait'],'exit':11,'stderr':'failed'})
        with patch.object(loop.AnxClient,'ask',side_effect=OSError('ANX temporarily down')) as failing:
            for _ in range(3): self.cycle()
        first = failing.call_args.args[1]
        self.respond()
        self.assertEqual(self.cycle(),0)
        asks=[c for c in self.calls() if c['tool'] == 'anx' and 'ask' in c['args']]
        self.assertEqual(len(asks),1)
        self.assertIn(first['situation'], asks[0]['args'])  # the journaled payload, not a fresh one
        self.assertIn(first['details'], asks[0]['args'])
