import contextlib
import datetime as dt
import fcntl
import io
import json
from unittest.mock import patch
from coordkit import wait
from tests.support import FakeCase


class WaitTests(FakeCase):
    def setUp(self):
        super().setUp()
        self.manifest.wait.anx_answers=False
        with contextlib.redirect_stdout(io.StringIO()):
            wait.main(['pending'],self.manifest)

    def state(self):
        return wait.State('lead')

    def test_events_dedupe_partial_and_truncation(self):
        p=self.manifest.paths.events; p.parent.mkdir(parents=True)
        row={'kind':'terminal','execution_id':'exec-1','label':'worker','state':'completed'}
        p.write_text(json.dumps(row)+'\n'+json.dumps(row)+'\n{"kind":')
        s=self.state()
        self.assertEqual(len(wait.scan_events(s,[str(p)],True)),1)
        self.assertEqual(wait.scan_events(s,[str(p)],True),[])
        p.write_text(json.dumps(dict(row,execution_id='e2'))+'\n')
        self.assertEqual(len(wait.scan_events(s,[str(p)],True)),1)

    def test_bus_repeat_window(self):
        p=self.manifest.paths.bus_log; p.parent.mkdir(parents=True)
        p.write_text(''); s=self.state()
        wait.scan_bus(s,p,'lead',None,True)
        r={'ts':'2000-01-01T00:00:00Z','frm':'worker','to':'lead','kind':'handoff','topic':'x','text':'same'}
        p.write_text(json.dumps(r)+'\n')
        self.assertIn('LEAD_WAIT bus ',wait.scan_bus(s,p,'lead',None,True)[0])
        with p.open('a') as f: f.write(json.dumps(dict(r,ts='2000-01-01T00:01:00Z'))+'\n')
        self.assertIn('bus-repeat',wait.scan_bus(s,p,'lead',None,True)[0])

    def test_three_way_deadline_merge(self):
        a=self.state(); wait.arm_deadline(a,'2030-01-01T00:00:00Z','old'); a.save()
        waiter=self.state(); writer=self.state()
        wait.arm_deadline(writer,'2030-01-02T00:00:00Z','new'); writer.save(only=('deadlines',))
        waiter.data['deadlines']=[]; waiter.data['fired']=['old@2030-01-01T00:00:00Z']; waiter.data['offsets']={'events':100}; waiter.save()
        self.assertEqual([d['label'] for d in self.state().data['deadlines']],['new'])
        writer.save(only=('deadlines',))
        final=self.state().data
        self.assertEqual(final['offsets'],{'events':100})
        self.assertEqual([d['label'] for d in final['deadlines']],['new'])

    def test_pending_never_writes_or_calls_network(self):
        out=io.StringIO()
        for _ in range(2):
            with contextlib.redirect_stdout(out):
                self.assertEqual(wait.main(['pending','--brief'],self.manifest),0)
        self.assertFalse(self.manifest.paths.root.exists()); self.assertEqual(self.calls(),[])

    def test_ci_verdict_head_transition(self):
        s=self.state()
        with patch.object(wait,'ci_verdict',side_effect=[('head1','green',''),('head1','green',''),('head2','failed','')]):
            self.assertEqual(len(wait.scan_ci(s,[1],'o/r',True,set())),1)
            self.assertEqual(wait.scan_ci(s,[1],'o/r',True,set()),[])
            self.assertIn('failed',wait.scan_ci(s,[1],'o/r',True,set())[0])

    def test_anx_answers_and_past_promise(self):
        self.respond({'tool':'anx','contains':['await'],'json':{'ok':True,'result':{'answers':[{'ask_id':'a','answer':{'text':'yes'}}]}}},
                     {'tool':'anx','contains':['orient'],'json':{'ok':True,'result':{'past_promise':[{'handle':'loop-a'}]}}})
        found=wait.scan_anx(set())
        self.assertIn('anx-answer',found[0]); self.assertIn('past-promise',found[1])
        self.assertTrue(all('anx' in c['args'] and 'ck-sample-lead' in c['args'] for c in self.calls()))

    def test_stale_lane_and_handoff_suppression(self):
        p=self.manifest.paths.events; p.parent.mkdir(parents=True)
        row={'kind':'launched','execution_id':'exec-1','label':'worker','ts':'2026-01-01T00:00:00Z'}
        p.write_text(json.dumps(row)+'\n')
        with patch.object(wait.AgentctlClient,'status',return_value={'state':'failed'}):
            self.assertEqual(len(wait.scan_stale(self.state(),[str(p)],False)),1)
            b=self.manifest.paths.bus_log
            b.write_text(json.dumps({'kind':'handoff','frm':'worker','ts':'2026-01-02T00:00:00Z'})+'\n')
            self.assertEqual(wait.scan_stale(self.state(),[str(p)],False,str(b)),[])

    def test_tiers_and_ask_guard_fail_open(self):
        cfg={'debounce':300}
        for line,tier in [('LEAD_WAIT bus w fail/x','immediate'),('LEAD_WAIT bus w handoff/x','debounced'),('LEAD_WAIT bus-status w handoff/x','ride'),('LEAD_WAIT ci #1 failed','immediate')]:
            self.assertEqual(wait.tier_of(line,cfg),tier)
        with patch('sys.stdin',io.StringIO('bad hook')):
            self.assertEqual(wait.main(['ask-guard'],self.manifest),0)

    def test_waiter_lock_for_every_wait(self):
        s=self.state(); s.dir.mkdir(parents=True)
        with (s.dir/'waiter.lock').open('a') as held:
            fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(wait.main(['--timeout','0'],self.manifest),4)
