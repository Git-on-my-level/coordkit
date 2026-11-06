import contextlib
import copy
import io
import json
from pathlib import Path
from unittest.mock import patch

from coordkit import cli, incumbent, shadow
from coordkit.extract import REDACT, REMINDER
from coordkit.manifest import load, ManifestError
from coordkit.state import read_json
from tests.support import FakeCase

FIXTURES=Path(__file__).parent/'fixtures'


class ShadowTests(FakeCase):
    def setUp(self):
        super().setUp()
        source=Path(incumbent.__file__)
        self.file.write_text('mode = "shadow"\n'+self.file.read_text()+f'\n[shadow]\nincumbent_bus_source = "{source}"\nincumbent_merge_source = "{source}"\n')
        self.manifest=load(self.file)
        self.manifest.paths.bus_log.write_text('') if self.manifest.paths.bus_log.exists() else None
        self.data={'bus':[],'events':[],'executions':[],'statuses':{},'cards':[], 'leases':{},'orient':{},'prs':[]}

    def quiet(self, fn, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            return fn(*args,**kwargs)

    def sample(self,at):
        with patch.object(shadow,'collect',return_value=self.data):
            return self.quiet(shadow.single_pass,self.manifest,at=at)

    def state(self):
        return read_json(self.manifest.paths.root/'shadow/state.json')

    def test_synthetic_tail_protocol_parity_and_redaction(self):
        self.data['bus']=shadow.read_rows(FIXTURES/'synthetic-bus-tail.jsonl')
        self.data['events']=shadow.normalize_events(shadow.read_rows(FIXTURES/'synthetic-events-tail.ndjson'))
        self.assertEqual(len(self.data['bus']),80)
        for name in ('synthetic-bus-tail.jsonl','synthetic-events-tail.ndjson'):
            text=(FIXTURES/name).read_text()
            self.assertLessEqual(len(text.splitlines()),300)
            for pattern,replacement in REDACT:
                self.assertIsNone(pattern.search(text),name)
            self.assertIsNone(REMINDER.search(text))
        ours,theirs=shadow.views(self.manifest,self.data,1_800_000_000)
        self.assertEqual(ours,theirs)
        self.assertGreater(len(ours['rulings'])+len(ours['blockers']),0)
        self.assertEqual(self.sample(1_800_000_000),0)
        self.assertEqual(self.calls(),[])

    def test_obligation_and_phase_owner_diffs(self):
        self.data['events']=[{'kind':'launched','label':'worker','execution_id':'exec-1','card':'card:task'}]
        self.data['executions']=[{'id':'exec-1','labels':['worker','anx.card.task'],'state':'running'}]
        self.data['cards']=[{'handle':'task','column_key':'review'}]
        self.data['leases']={'card:task':{'owner':'actor:other'}}
        self.assertEqual(self.sample(100),5)
        self.assertEqual(self.state()['diffs'][0]['code'],'LANE_PHASE_OWNER')
        self.assertIsNone(self.state()['clean_since'])
        self.data['cards'][0]['column_key']='in_progress'
        self.data['leases']['card:task']['owner']='worker'
        self.assertEqual(self.sample(200),0)

    def test_holds_and_narrow_whitelist(self):
        rows=[{'id':'h','frm':'lead','kind':'msg','text':'hold #123 until ready','ts':'2026-01-01T00:00:00Z'}]
        self.data['bus']=rows
        ours,theirs=shadow.views(self.manifest,self.data,100)
        theirs['holds']['123']=[]
        self.assertEqual(shadow.differences(self.data,ours,theirs,100)[0]['whitelist'],'LEAD_FREE_TEXT_HOLD')
        rows[0]['frm']='worker'
        ours['holds']['123']=['h']
        self.assertIsNone(shadow.differences(self.data,ours,theirs,100)[0]['whitelist'])
        rows[0].update(frm='lead',kind='hold',prs=['123'])
        self.assertIsNone(shadow.differences(self.data,ours,theirs,100)[0]['whitelist'])

    def test_capacity_churn_never_whitelists_owner_change(self):
        self.data['events']=[{'card':'card:t','execution_id':'e1','kind':'terminal','ts':'1970-01-01T00:01:20Z'},
                             {'card':'card:t','execution_id':'e2','kind':'relaunched','ts':'1970-01-01T00:01:30Z'}]
        ours={'rulings':[],'blockers':[],'holds':{},'loops':{},'lanes':{'card:t':{'phase':'in_progress','owner':'worker'}}}
        theirs=copy.deepcopy(ours); theirs['lanes']['card:t']['phase']='review'
        self.assertEqual(shadow.differences(self.data,ours,theirs,100)[0]['whitelist'],'CAPACITY_RELAUNCH_CHURN')
        theirs['lanes']['card:t']['owner']='other'
        self.assertIsNone(shadow.differences(self.data,ours,theirs,100)[0]['whitelist'])
        theirs['lanes']['card:t']['owner']='worker'
        self.assertIsNone(shadow.differences(self.data,ours,theirs,1000)[0]['whitelist'])

    def test_loop_receipt_unknown_is_a_mismatch(self):
        self.data['cards']=[{'handle':'loop-audit','cadence':{'every_seconds':60},'last_receipt_at':'1970-01-01T00:00:10Z'}]
        self.data['orient']={'past_promise':[{'handle':'loop-audit'}]}
        self.assertEqual(self.sample(100),0)
        del self.data['cards'][0]['last_receipt_at']
        self.assertEqual(self.sample(101),5)
        self.assertEqual(self.state()['diffs'][0]['code'],'LOOP_CADENCE')

    def test_24_hours_required_with_freshness_gap_and_failure_reset(self):
        for at in range(0,86401,600):
            self.assertEqual(self.sample(at),0)
        self.assertTrue(self.state()['eligible'])
        self.assertEqual(self.quiet(shadow.report,self.manifest,at=86401),0)
        self.assertEqual(self.quiet(shadow.report,self.manifest,at=87001),5)
        self.sample(87001)
        self.assertEqual(self.state()['clean_seconds'],0)
        with patch.object(shadow,'collect',side_effect=OSError('source missing')):
            self.assertEqual(self.quiet(shadow.single_pass,self.manifest,at=87002),5)
            # One on-time unavailable sample is unobserved and keeps the streak, never eligible.
            self.assertEqual(self.state()['clean_since'],87001)
            self.assertTrue(self.state()['unobserved'])
            self.assertFalse(self.state()['eligible'])
            self.assertEqual(self.quiet(shadow.single_pass,self.manifest,at=87003),5)
        self.assertIsNone(self.state()['clean_since'])
        self.sample(87004)
        self.assertEqual(self.state()['clean_seconds'],0)
        self.sample(87005)
        with patch.object(shadow,'collect',side_effect=OSError('source missing')):
            self.quiet(shadow.single_pass,self.manifest,at=87006)
        self.sample(87007)
        self.assertEqual(self.state()['clean_since'],87004)
        # A real mismatch is never bridged.
        with patch.object(shadow,'differences',return_value=[{'code':'LANE_PHASE_OWNER','whitelist':None}]):
            self.sample(87008)
        self.assertIsNone(self.state()['clean_since'])

    def test_overrunning_sample_is_journaled_unavailable(self):
        self.sample(0)
        slow=lambda manifest: __import__('time').sleep(5)
        with patch.object(shadow,'SAMPLE_SECONDS',1),patch.object(shadow,'collect',side_effect=slow):
            self.assertEqual(self.quiet(shadow.single_pass,self.manifest,at=300),5)
        self.assertEqual(self.state()['diffs'][0]['code'],'SOURCE_UNAVAILABLE')
        self.assertTrue(self.state()['unobserved'])
        self.assertEqual(self.state()['clean_since'],0)

    def test_journal_recovers_lost_state_save_without_losing_dirty_sample(self):
        self.sample(0); self.sample(100)
        with patch.object(shadow,'collect',side_effect=OSError('bad source')),patch.object(shadow,'write_json',side_effect=OSError('crash')):
            with self.assertRaises(OSError): self.quiet(shadow.single_pass,self.manifest,at=200)
        self.sample(300)
        # The journaled failure was a single on-time unavailable sample, so it was bridged.
        self.assertEqual(self.state()['clean_since'],0)
        self.assertEqual(len(shadow.read_rows(self.manifest.paths.root/'shadow/diffs.jsonl')),4)

    def test_config_changes_invalidate_report_and_restart_streak(self):
        original = copy.deepcopy(self.manifest)
        for at in range(0,86401,600):
            self.sample(at)
        journal = self.manifest.paths.root/'shadow/diffs.jsonl'
        eligible = journal.read_bytes()
        changes = [('lane_records',lambda m:setattr(m.shadow,'lane_records','other-req')),
                   ('receipt_prefix',lambda m:setattr(m.loop,'receipt_prefix','audit-')),
                   ('lead_actor',lambda m:setattr(m.anx,'lead_actor','other-lead')),
                   ('base_url',lambda m:setattr(m.anx,'base_url','https://other.invalid')),
                   ('source',lambda m:setattr(m.shadow,'incumbent_bus_source',str(self.root/'copy.py')))]
        (self.root/'copy.py').write_bytes(Path(incumbent.__file__).read_bytes())
        for name,change in changes:
            with self.subTest(name=name):
                self.manifest = copy.deepcopy(original)
                journal.write_bytes(eligible)
                self.assertEqual(self.quiet(shadow.report,self.manifest,at=86401),0)
                change(self.manifest)
                before = journal.read_bytes()
                self.assertEqual(self.quiet(shadow.report,self.manifest,at=86401),5)
                self.assertEqual(journal.read_bytes(),before)
                self.assertEqual(self.sample(86401),0)
                self.assertEqual(self.state()['clean_since'],86401)
                self.assertFalse(self.state()['eligible'])

    def test_cursor_only_card_snapshot_fails_closed(self):
        self.manifest.paths.bus_log.parent.mkdir(parents=True)
        self.manifest.paths.bus_log.write_text('')
        self.manifest.paths.events.write_text('')
        self.respond({'tool':'anx','contains':['cards','list'],
                      'json':{'ok':True,'result':{'cards':[],'next_cursor':'page-two'}}})
        self.assertEqual(self.quiet(shadow.single_pass,self.manifest,at=100),5)
        self.assertIn('incomplete_card_snapshot',self.state()['diffs'][0]['detail'])
        self.assertIsNone(self.state()['clean_since'])
        self.assertFalse(self.state()['eligible'])

    def test_lane_record_symlink_retarget_resets_streak(self):
        old, new, link = self.root/'old-req', self.root/'new-req', self.root/'req'
        old.mkdir(); new.mkdir(); link.symlink_to(old,target_is_directory=True)
        self.manifest.shadow.lane_records = 'req'
        self.sample(0); self.sample(100)
        self.assertEqual(self.state()['clean_seconds'],100)
        link.unlink(); link.symlink_to(new,target_is_directory=True)
        self.sample(200)
        self.assertEqual(self.state()['clean_since'],200)

    def test_cli_run_is_one_pass_and_short_target_never_reduces_24h(self):
        with patch.object(shadow,'collect',return_value=self.data) as collect:
            self.assertEqual(self.quiet(cli.main,['--manifest',str(self.file),'shadow','run','--hours','1']),0)
        collect.assert_called_once()
        self.assertFalse(self.state()['eligible'])
        self.assertEqual(self.state()['required_seconds'],86400)
        self.assertEqual(self.state()['target_seconds'],3600)
        with self.assertRaises(SystemExit): self.quiet(shadow.main,['run','--hours','nan'],self.manifest)

    def test_collect_only_allowed_verbs_and_preserves_input_bytes(self):
        self.manifest.paths.bus_log.parent.mkdir(parents=True,exist_ok=True)
        self.manifest.paths.bus_log.write_bytes((FIXTURES/'synthetic-bus-tail.jsonl').read_bytes())
        self.manifest.paths.events.write_bytes((FIXTURES/'synthetic-events-tail.ndjson').read_bytes())
        before={p:p.read_bytes() for p in (self.manifest.paths.bus_log,self.manifest.paths.events)}
        self.respond({'tool':'anx','contains':['cards','list'],'json':{'ok':True,'result':{'cards':[]}}})
        self.assertEqual(self.quiet(shadow.main,['diff'],self.manifest),0)
        self.assertEqual(before,{p:p.read_bytes() for p in before})
        for call in self.calls():
            args=call['args']
            if call['tool']=='anx': self.assertTrue('list' in args or 'show' in args or 'orient' in args)
            elif call['tool']=='agentctl': self.assertTrue('recent' in args or 'status' in args)
            else: self.assertEqual(args[:2],['pr','list'])

    def test_source_drift_and_missing_files_break_streak(self):
        source=self.root/'changed.py'; source.write_text('def open_rulings(rows): return []\n')
        self.manifest.shadow.incumbent_bus_source=str(source)
        self.assertEqual(self.quiet(shadow.single_pass,self.manifest),5)
        self.assertIn('INCUMBENT_PROTOCOL_DRIFT',self.state()['diffs'][0]['detail'])
        self.assertFalse(self.state()['eligible'])

    def test_shadow_output_refuses_escape_and_report_is_read_only(self):
        self.assertEqual(self.quiet(shadow.report,self.manifest),5)
        self.assertFalse(self.manifest.paths.root.exists())
        self.manifest.paths.root=self.root.parent
        with self.assertRaisesRegex(ManifestError,'manifest_local'):
            shadow.single_pass(self.manifest)
        self.manifest.paths.root=self.root/'.coordkit'
        self.manifest.paths.root.mkdir()
        (self.manifest.paths.root/'shadow').symlink_to(self.root/'elsewhere',target_is_directory=True)
        with self.assertRaisesRegex(ManifestError,'symlink_escape'):
            shadow.single_pass(self.manifest)


class ShadowIncumbentQuirkTests(FakeCase):
    def setUp(self):
        super().setUp()
        self.manifest=load(self.file)
        self.data={'bus':[],'events':[],'executions':[],'statuses':{},'cards':[],'leases':{},'orient':{},'prs':[]}

    def lane(self, ours_phase, ours_owner, lease, executions=()):
        self.data['leases']={'card:t':lease}
        self.data['executions']=list(executions)
        ours={'rulings':[],'blockers':[],'holds':{},'loops':{},'lanes':{'card:t':{'phase':ours_phase,'owner':ours_owner}}}
        theirs=copy.deepcopy(ours); theirs['lanes']['card:t']={'phase':'backlog','owner':'w'}
        return shadow.differences(self.data,ours,theirs,100)[0]['whitelist']

    def test_stale_incumbent_projection_whitelist_is_narrow(self):
        expired={'state':'expired','owner':'actor:w'}
        self.assertEqual(self.lane('review','lead',expired),'INCUMBENT_STALE_PROJECTION')
        self.assertEqual(self.lane('review','lead',{'card_missing':True}),'INCUMBENT_STALE_PROJECTION')
        running={'id':'e','state':'running','labels':['w','anx.card.t']}
        self.assertEqual(self.lane('in_progress','w',expired,[running]),'INCUMBENT_STALE_PROJECTION')
        # A live lease, a lane still running after coordkit says review, or an in-progress lane
        # with nothing running stay blocking.
        self.assertIsNone(self.lane('review','lead',{'state':'held','owner':'actor:w'}))
        self.assertIsNone(self.lane('review','lead',expired,[running]))
        self.assertIsNone(self.lane('in_progress','w',expired))

    def test_failed_or_overdue_lane_projects_ready_to_lead(self):
        expired={'state':'expired','owner':'actor:w'}
        self.assertEqual(self.lane('ready','lead',expired),'INCUMBENT_STALE_PROJECTION')
        self.data['cards']=[{'handle':'t','column_key':'backlog'},{'handle':'u','column_key':'backlog'}]
        self.data['events']=[
            {'label':'w','execution_id':'e1','card':'card:t','kind':'launched','ts':'1970-01-01T00:00:00Z'},
            {'label':'v','execution_id':'e2','card':'card:u','kind':'terminal','state':'failed','handoff':'lead'}]
        self.data['statuses']={'e1':{'state':'running','started_at':'1970-01-01T00:00:00Z'}}
        hours=self.manifest.lane.max_run_hours*3600
        ours,_=shadow.views(self.manifest,self.data,hours-1)
        self.assertEqual(ours['lanes']['card:t'],{'phase':'in_progress','owner':'w'})
        self.assertEqual(ours['lanes']['card:u']['phase'],'ready')
        # Past max_run_hours coordkit would have expired the lane, so a hung execution is not progress.
        ours,_=shadow.views(self.manifest,self.data,hours)
        self.assertEqual(ours['lanes']['card:t']['phase'],'ready')
        self.assertNotEqual(ours['lanes']['card:t']['owner'],'w')
        # An unreachable run is expired after hung_after, well before max_run_hours.
        self.data['statuses']['e1'].update(liveness='unreachable',updated_at='1970-01-01T00:00:00Z')
        ours,_=shadow.views(self.manifest,self.data,3*3600)
        self.assertEqual(ours['lanes']['card:t']['phase'],'ready')
        self.data['statuses']['e1']['liveness']='alive'
        ours,_=shadow.views(self.manifest,self.data,3*3600)
        self.assertEqual(ours['lanes']['card:t']['phase'],'in_progress')

    def test_umbrella_card_lanes_rank_and_hung_runs_are_not_live(self):
        self.data['cards']=[{'handle':'t','column_key':'in_progress'}]
        self.data['events']=[
            {'label':'done','execution_id':'e1','card':'card:t','kind':'terminal','state':'completed','handoff':'lead'},
            {'label':'live','execution_id':'e2','card':'card:t','kind':'launched','ts':'1970-01-01T00:00:00Z'},
            {'label':'late','execution_id':'e3','card':'card:t','kind':'terminal','state':'failed','handoff':'lead'}]
        self.data['statuses']={'e2':{'state':'running','liveness':'alive','started_at':'1970-01-01T00:00:00Z'}}
        ours,_=shadow.views(self.manifest,self.data,60)
        self.assertEqual(ours['lanes']['card:t'],{'phase':'in_progress','owner':'live'})
        self.data['statuses']['e2'].update(liveness='unreachable',updated_at='1970-01-01T00:00:00Z')
        ours,_=shadow.views(self.manifest,self.data,3*3600)
        self.assertEqual(ours['lanes']['card:t']['phase'],'review')
        # A hung run carrying the card label does not block the stale-projection whitelist.
        hung={'id':'e2','state':'running','liveness':'unreachable','started_at':'1970-01-01T00:00:00Z','labels':['live','anx.card.t']}
        self.data['leases']={'card:t':{'state':'expired','owner':'actor:lead'}}
        self.data['executions']=[hung]
        mine={'rulings':[],'blockers':[],'holds':{},'loops':{},'lanes':{'card:t':{'phase':'review','owner':'lead'}}}
        theirs={'rulings':[],'blockers':[],'holds':{},'loops':{},'lanes':{'card:t':{'phase':'in_progress','owner':'lead'}}}
        self.assertEqual(shadow.differences(self.data,mine,theirs,3*3600,self.manifest)[0]['whitelist'],'INCUMBENT_STALE_PROJECTION')
        self.assertIsNone(shadow.differences(self.data,mine,theirs,3*3600)[0]['whitelist'])

    def test_loop_cadence_uses_orient_promise_and_skips_resolved(self):
        self.data['cards']=[{'handle':'loop-a'},{'handle':'loop-b','resolution':'done'}]
        self.data['orient']={'loops':[{'ref':'card:loop-a','promise':{'source':'cadence','at':'1970-01-01T00:01:00Z'}}]}
        ours,theirs=shadow.views(self.manifest,self.data,30)
        self.assertEqual(ours['loops'],{'card:loop-a':{'past_due':False}})
        ours,_=shadow.views(self.manifest,self.data,120)
        self.assertTrue(ours['loops']['card:loop-a']['past_due'])
