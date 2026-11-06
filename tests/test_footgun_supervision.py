import contextlib
import fcntl
import io
import json
import signal
import time
from unittest.mock import patch
from coordkit import lane, loop, wait
from coordkit.process import ToolError
from coordkit.state import read_json, write_json
from tests.support import FakeCase


class SupervisionTests(FakeCase):
    def quiet(self, fn, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return fn(*args, **kwargs)

    def plan(self, started=0):
        path=self.manifest.paths.out_dir/'req/worker.plan.json'
        plan=dict(label='worker',key='ck-worker',started=started,plan_file=str(path),card='card:task',
                  execution_id='exec-stuck',phase='running',prompt='complete',pool='implement',owns=[],access='read_only',bus_start=0,autocontinued=True)
        write_json(path,plan)
        return plan

    def test_p2_5_deadline_cancels_no_renew_asks_and_finishes(self):
        plan=self.plan()
        with patch.object(lane.AgentctlClient,'status',side_effect=[{'state':'running'},{'state':'cancelled'}]):
            self.assertEqual(self.quiet(lane.watch,self.manifest,plan),1)
        self.assertFalse(any('renew' in c['args'] for c in self.calls()))
        self.assertEqual(len([c for c in self.calls() if 'ask' in c['args']]),1)
        self.assertTrue(any('cancel' in c['args'] for c in self.calls()))
        self.assertTrue(any('release' in c['args'] for c in self.calls()))
        self.assertEqual(read_json(__import__('pathlib').Path(plan['plan_file']))['terminal'],'failed')
        # The failed lane's card returns to ready instead of looking worked on.
        self.assertTrue(any('cards' in c['args'] and 'move' in c['args'] and 'ready' in c['args'] for c in self.calls()))

    def test_p2_5_unconfirmed_cancel_keeps_lease_and_stops_renewing(self):
        plan=self.plan()
        with patch.object(lane.AgentctlClient,'status',return_value={'state':'running'}):
            self.quiet(lane.recover,self.manifest,plan)
            self.quiet(lane.recover,self.manifest,plan)
        self.assertFalse(any('renew' in c['args'] or 'release' in c['args'] for c in self.calls()))
        saved=read_json(__import__('pathlib').Path(plan['plan_file']))
        self.assertEqual(saved['completion']['state'],'failed')
        self.assertNotIn('terminal',saved)
        self.assertEqual(len([c for c in self.calls() if 'ask' in c['args'] and 'deadline_exceeded' in ' '.join(c['args'])]),1)

    def test_unreachable_run_expires_after_hung_after(self):
        plan=self.plan(started=time.time())
        stamp=lambda age: __import__('datetime').datetime.fromtimestamp(time.time()-age,__import__('datetime').timezone.utc).isoformat()
        fresh={'state':'running','liveness':'unreachable','updated_at':stamp(60)}
        self.assertFalse(lane.hung(self.manifest,fresh))
        self.assertFalse(lane.hung(self.manifest,{'state':'running','liveness':'alive','updated_at':stamp(86400)}))
        self.assertFalse(lane.hung(self.manifest,{'state':'running','liveness':'unreachable'}))
        stale={'state':'running','liveness':'unreachable','updated_at':stamp(3*3600)}
        self.assertTrue(lane.hung(self.manifest,stale))
        # Naive stamps are UTC, never host-local time (hosts can use different time zones).
        naive=__import__('datetime').datetime.fromtimestamp(time.time()-3600,__import__('datetime').timezone.utc).replace(tzinfo=None).isoformat()
        self.assertFalse(lane.hung(self.manifest,{'state':'running','liveness':'unreachable','updated_at':naive}))
        # A run that answers on the re-read is not cancelled.
        # Check the supervisor without racing the real detached watcher against
        # the next status scenario. The detached CLI/lock lifecycle is tested separately.
        with patch.object(lane, 'detach_watcher') as restored, \
                patch.object(lane.AgentctlClient,'status',side_effect=[stale,fresh]):
            self.assertTrue(self.quiet(lane.recover,self.manifest,plan))
        restored.assert_called_once_with(self.manifest, plan)
        self.assertFalse(any('cancel' in c['args'] for c in self.calls()))
        with patch.object(lane.AgentctlClient,'status',side_effect=[stale,stale,stale,{'state':'cancelled'}]):
            self.assertTrue(self.quiet(lane.recover,self.manifest,plan))
        self.assertTrue(any('cancel' in c['args'] for c in self.calls()))
        self.assertTrue(any('ask' in c['args'] and 'execution_hung' in ' '.join(c['args']) for c in self.calls()))
        self.assertEqual(read_json(__import__('pathlib').Path(plan['plan_file']))['terminal'],'failed')

    def test_p2_6_unknown_recovery_asks_once(self):
        plan=self.plan(time.time())
        with patch.object(lane.AgentctlClient,'status',return_value={}):
            self.quiet(lane.recover,self.manifest,plan)
            self.quiet(lane.recover,self.manifest,plan)
        self.assertEqual(len([c for c in self.calls() if 'ask' in c['args']]),1)
        self.assertFalse(any('renew' in c['args'] for c in self.calls()))

    def test_p2_6_capacity_exhaustion_asks(self):
        plan=self.plan(time.time()); self.manifest.lane.capacity_relaunch_max=0
        with patch.object(lane.AgentctlClient,'await_result',side_effect=[ToolError('agentctl',10,'capacity'),'recovered']), \
             patch.object(lane.AgentctlClient,'status',return_value={'state':'attention','failure':{'code':'provider_capacity'}}):
            self.assertEqual(self.quiet(lane.watch,self.manifest,plan),0)
        self.assertTrue(any('ask' in c['args'] and 'capacity_exhausted' in ' '.join(c['args']) for c in self.calls()))

    def test_p3_9_skips_count_and_escalate_then_success_clears(self):
        state=self.manifest.paths.root/'loops/audit'; state.mkdir(parents=True)
        with (state/'cycle.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            for n in range(1,4):
                self.assertEqual(self.quiet(loop.run_cycle,self.manifest,'audit'),4)
                self.assertEqual(int((state/'fails').read_text()),n)
        self.assertEqual(len([c for c in self.calls() if 'ask' in c['args']]),1)
        self.assertEqual(read_json(state/'skip.json')['reason'],'cycle_lock_held')
        self.assertEqual(self.quiet(loop.run_cycle,self.manifest,'audit'),0)
        self.assertEqual(int((state/'fails').read_text()),0)
        self.assertFalse((state/'escalation.json').exists())
        self.assertEqual((state/'cycle.lock').read_text(),'')

    def test_loop_whole_cycle_deadline(self):
        self.manifest.loop.max_age='1s'
        # Deliver the configured timer's signal at the boundary under test;
        # assert its actual arming/disarming without waiting out wall time.
        with patch.object(loop,'snapshot',side_effect=lambda m:signal.raise_signal(signal.SIGALRM)), \
             patch.object(loop.signal,'setitimer',wraps=signal.setitimer) as timer:
            self.assertEqual(self.quiet(loop.run_cycle,self.manifest,'audit'),1)
        self.assertEqual(timer.call_args_list[0].args,(signal.ITIMER_REAL,1))
        self.assertEqual(timer.call_args_list[-1].args,(signal.ITIMER_REAL,0))
        state=self.manifest.paths.root/'loops/audit'
        self.assertIn('max_age exceeded',next(state.glob('runs/*/err.txt')).read_text())
        self.assertEqual(int((state/'fails').read_text()),1)

    def test_p3_14_stop_resolves_card_and_blocks_cycles(self):
        self.assertEqual(self.quiet(loop.main,['stop','audit','--reason','retired by lead'],self.manifest),0)
        self.assertTrue(any('resolve' in c['args'] and 'card:loop-audit' in c['args'] for c in self.calls()))
        calls=len(self.calls())
        self.assertEqual(self.quiet(loop.run_cycle,self.manifest,'audit'),0)
        self.assertEqual(len(self.calls()),calls)

    def test_p3_15_stale_alert_refires_after_age_and_clears(self):
        self.quiet(wait.main,['pending'],self.manifest)
        state=wait.State('lead')
        records={'exec-dead':{'label':'worker','ts':'1970-01-01T00:00:00Z','card':'card:task'}}
        with patch.object(wait,'in_flight',return_value=records), \
             patch.object(wait.AgentctlClient,'status',return_value={'state':'failed','liveness':'exited'}):
            with patch.object(wait.time,'time',return_value=10000):
                self.assertTrue(wait.scan_stale(state,[],True))
                self.assertEqual(wait.scan_stale(state,[],False),[])
            with patch.object(wait.time,'time',return_value=13600):
                self.assertTrue(wait.scan_stale(state,[],True))
        with patch.object(wait,'in_flight',return_value=records), \
             patch.object(wait.AgentctlClient,'status',return_value={'state':'failed','liveness':'exited'}), \
             patch.object(wait,'bus_activity',return_value={'worker':(20000,[20000])}):
            self.assertEqual(wait.scan_stale(state,[],True),[])
        self.assertEqual(state.data['stale_times'],{})
        state.data['stale_times']['exec-dead']=13600
        with patch.object(wait,'in_flight',return_value={}): wait.scan_stale(state,[],True)
        self.assertEqual(state.data['stale_times'],{})

    def test_cycle_deadline_kills_nested_tool_process(self):
        import os
        import signal
        import sys
        from coordkit.process import run
        pidfile=self.root/'tool.pid'
        # The child signals only after writing its identity. This removes a race
        # where the timer expired during an earlier fake card lookup, while still
        # exercising real signal delivery and process-group termination/reaping.
        code="import os,sys,signal; open(sys.argv[1],'w').write(str(os.getpid())); os.kill(int(sys.argv[2]),signal.SIGALRM); signal.pause()"
        self.manifest.loop.max_age='30s'
        def nested(manifest):
            return run([sys.executable,'-S','-c',code,str(pidfile),str(os.getpid())],timeout=2).stdout
        try:
            with patch.object(loop,'snapshot',side_effect=nested):
                self.assertEqual(self.quiet(loop.run_cycle,self.manifest,'audit'),1)
            pid=int(pidfile.read_text())
            with self.assertRaises(ProcessLookupError): os.kill(pid,0)
        finally:
            if pidfile.exists():
                pid=int(pidfile.read_text())
                try: os.killpg(pid,signal.SIGKILL)
                except ProcessLookupError: pass

    def test_proven_completion_replays_after_deadline_without_relabel(self):
        from pathlib import Path
        plan=self.plan()
        output=self.manifest.paths.out_dir/'worker.out'; output.write_text('completed before crash')
        plan['completion']={'rc':0,'state':'completed','out':str(output)}
        plan['phase']='completing'
        write_json(Path(plan['plan_file']),plan)
        self.assertTrue(self.quiet(lane.recover,self.manifest,plan))
        self.assertEqual(read_json(Path(plan['plan_file']))['terminal'],'completed')
        self.assertFalse(any('cancel' in c['args'] or 'ask' in c['args'] for c in self.calls()))
