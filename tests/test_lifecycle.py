import contextlib
import io
import json
from pathlib import Path
import plistlib
from unittest.mock import patch
from coordkit import services, cli, lifecycle
from coordkit.anxcli import AnxClient
from coordkit.manifest import load, ManifestError
from coordkit.process import ToolError
from tests.support import FakeCase


class LifecycleTests(FakeCase):
    def setUp(self):
        super().setUp()
        self.manifest.launchd.path=[str(Path.home()/'.local/bin'),'/usr/bin','/bin']
        self.manifest.loop.loops={'audit':{}}
        self.good=[{'tool':'anx','contains':['orient'],'json':{'ok':True,'result':{'principal':{'id':'lead'}}}},
                   {'tool':'anx','contains':['boards','get'],'json':{'ok':True,'result':{'board':{'id':'board'}}}},
                   {'tool':'agentctl','contains':['config','doctor'],'json':{'ok':True,'result':{'valid':True}}}]
        self.respond(*self.good)

    def quiet(self,fn,*args,**kwargs):
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            return fn(*args,**kwargs)

    def test_init_creates_missing_board_and_loop_idempotently(self):
        self.respond({'tool':'anx','contains':['boards','get'],'exit':2,'json':{'ok':False,'error':{'code':'not_found'}}},
                     {'tool':'anx','contains':['cards','get'],'exit':2,'json':{'ok':False,'error':{'code':'not_found'}}})
        self.assertEqual(self.quiet(lifecycle.initialize,'sample',self.file,'anx',None,'owner/repo'),0)
        # Existing manifest from FakeCase has no loops; provision a named loop for second phase.
        self.file.write_text(self.file.read_text()+'\n[loop.loops.audit]\n')
        self.assertEqual(self.quiet(lifecycle.initialize,'sample',self.file,'anx',None,'owner/repo'),0)
        creates=len([c for c in self.calls() if 'create' in c['args']])
        self.respond(*self.good)
        self.assertEqual(self.quiet(lifecycle.initialize,'sample',self.file,'anx',None,'owner/repo'),0)
        self.assertEqual(len([c for c in self.calls() if 'create' in c['args']]),creates)
        self.assertTrue(any('cadence' in c['args'] for c in self.calls()))
        self.assertTrue(all('--as' in c['args'] for c in self.calls() if c['tool']=='anx'))

    def test_init_scaffold_and_network_failure(self):
        target=self.root/'new/coordkit.toml'
        self.respond({'tool':'anx','contains':['orient'],'exit':2,'stderr':'unreachable'})
        with self.assertRaises(ToolError):
            self.quiet(lifecycle.initialize,'new',target,'anx',None,'owner/repo')
        self.assertFalse(target.exists())
        self.respond(*self.good)
        self.assertEqual(self.quiet(lifecycle.initialize,'new',target,'anx',None,'owner/repo'),0)
        self.assertEqual(load(target).project.name,'new')

    def test_plan_prints_plists_without_writes(self):
        before=set(self.root.rglob('*'))
        out=io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(lifecycle.up(self.manifest),0)
        self.assertIn('<plist',out.getvalue()); self.assertIn('UP_PLAN_ONLY',out.getvalue())
        self.assertEqual(set(self.root.rglob('*'))-{self.log},before)
        self.assertFalse(any('bridge' in c['args'] for c in self.calls()))
        for job in lifecycle.templates(self.manifest):
            self.assertTrue(job['Label'].startswith('com.coordkit.sample.'))
            self.assertIn(str(Path.home()/'.local/bin'),job['EnvironmentVariables']['PATH'])
            self.assertTrue(Path(job['ProgramArguments'][0]).is_absolute())

    def test_apply_and_down_only_recorded_jobs(self):
        with patch.object(services,'checked',return_value='[]') as checked:
            self.assertEqual(self.quiet(lifecycle.up,self.manifest,True),0)
        state=json.loads((self.manifest.paths.root/'lifecycle.json').read_text())
        self.assertEqual(len(state['jobs']),2)
        self.assertEqual(len([c for c in checked.call_args_list if 'bootstrap' in c.args[0]]),2)
        for job in state['jobs']:
            self.assertIn(str(Path.home()/'.local/bin'),plistlib.loads(Path(job['file']).read_bytes())['EnvironmentVariables']['PATH'])
        with patch.object(services.process,'run') as run,patch.object(services,'checked',return_value='') as checked:
            run.return_value.returncode=0
            self.assertEqual(self.quiet(lifecycle.down,self.manifest),0)
            self.assertEqual(self.quiet(lifecycle.down,self.manifest),0)
        self.assertEqual(len([c for c in checked.call_args_list if 'bootout' in c.args[0]]),2)

    def test_apply_installs_question_hooks_per_config_dir_and_down_restores_them(self):
        # briefs/question-escalation.md: up --apply installs the hooks under a receipt; down reverses them.
        claude2 = self.root/'claude-2'; claude2.mkdir()
        original = {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'agent-heartbeat touch'}]}]}, 'statusLine': {'x': 1}}
        (claude2/'settings.json').write_text(json.dumps(original))
        self.manifest.questions.config_dirs = [str(claude2)]
        with patch.object(services,'checked',return_value='[]'):
            self.assertEqual(self.quiet(lifecycle.up,self.manifest,True),0)
        installed = json.loads((claude2/'settings.json').read_text())
        self.assertEqual(installed['hooks']['PreToolUse'][0]['matcher'], 'AskUserQuestion')
        self.assertTrue(list((self.manifest.paths.root/'hook-backups').glob('claude-2-*.json')))
        from coordkit import hooks
        self.assertEqual(hooks.question_drift(self.manifest), [])
        with patch.object(services.process,'run') as run,patch.object(services,'checked',return_value=''):
            run.return_value.returncode=0
            self.assertEqual(self.quiet(lifecycle.down,self.manifest),0)
        self.assertEqual(json.loads((claude2/'settings.json').read_text()), original)

    def test_doctor_green_red_matrices(self):
        self.assertEqual(self.quiet(lifecycle.doctor,self.manifest),0)
        for response in [{'tool':'anx','contains':['orient'],'exit':2,'stderr':'offline'},
                         {'tool':'agentctl','contains':['config','doctor'],'json':{'ok':True,'result':{'valid':False}}},
                         {'tool':'gh','exit':1,'stderr':'auth'},
                         {'tool':'agentctl','contains':['anx','status'],'json':{'ok':True,'result':{'dead_letter_count':3}}}]:
            self.respond(response,*self.good)
            self.assertEqual(self.quiet(lifecycle.doctor,self.manifest),1)

    def test_adopt_persists_and_every_lead_call_uses_identity(self):
        self.assertEqual(self.quiet(lifecycle.adopt,self.manifest,'new-lead'),0)
        fresh=load(self.file)
        self.assertEqual(fresh.anx.lead_actor,'new-lead')
        c=AnxClient(fresh); c.call('cards','get','card:task'); c.note('card:task','proof',evidence='event:proof')
        self.assertTrue(all('new-lead' in c['args'] for c in self.calls()))

    def test_shadow_cli_refuses_all_writers_before_tools_or_state(self):
        self.file.write_text('mode = "shadow"\n'+self.file.read_text())
        writers=[['bus','post','--from','w','--to','lead','hi'],['bus','ack','id','--from','w'],
                 ['bus','wait','--to','w','--timeout','0'],['bus','wait','--to=w','--timeout=0'],
                 ['bus','--no-mirror','post','--from','w','--to','lead','hi'],['bus','inbox','w','--unread'],
                 ['bus','harvest','--mark'],['lane','launch','w','missing','--card','card:x'],
                 ['lane','adopt','--events','missing'],['loop','run','audit'],
                 ['reconcile'],['init','sample'],['up','--apply'],['down'],['lead','adopt']]
        for args in writers:
            self.assertEqual(self.quiet(cli.main,['--manifest',str(self.file),*args]),2,args)
        self.assertEqual(self.calls(),[]); self.assertFalse(self.manifest.paths.root.exists())

    def test_down_refuses_legacy_backup_without_edited_snapshot(self):
        target=self.root/'hooks.json'; target.write_text('modified')
        backup=self.root/'hooks.backup'; backup.write_text('original')
        self.manifest.paths.root.mkdir()
        (self.manifest.paths.root/'lifecycle.json').write_text(json.dumps({'jobs':[],
            'hook_backups':[{'target':str(target),'backup':str(backup)}]}))
        with self.assertRaisesRegex(ManifestError,'hook_rollback_snapshot_missing'):
            self.quiet(lifecycle.down,self.manifest)
        self.assertEqual(target.read_text(),'modified')

    def test_drift_warns_without_changing_doctor_exit_or_lane(self):
        from datetime import datetime, timezone
        from coordkit.manifest import UsageLane
        from coordkit.usage import transcript_dir
        lane = UsageLane('worker', str(self.root/'account'), '/work/repo', 'family', 'high')
        self.manifest.lanes = [lane]
        directory = transcript_dir(lane)
        directory.mkdir(parents=True)
        path = directory/'session.jsonl'
        row = dict(type='assistant', timestamp=datetime.now(timezone.utc).isoformat(),
                   message=dict(id='message-1', model='claude-other-1', usage={'input_tokens': 10}))
        path.write_text(json.dumps(row)+'\n')
        before = path.read_bytes()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(lifecycle.doctor(self.manifest), 0)
        self.assertIn('DOCTOR model_drift warn lane=worker intended=family actual=claude-other-1', out.getvalue())
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(any(c['tool'] == 'agentctl' and 'delegate' in c['args'] for c in self.calls()))


class CadenceDriftTests(FakeCase):
    """Real `anx cards get` has no cadence; it lives in `anx context` under work.cadence."""
    def setUp(self):
        super().setUp()
        self.manifest.loop.loops={'audit':{'default_cadence':'30m','progress_budget':'30m'}}
        self.card={'tool':'anx','contains':['cards','get','card:loop-audit'],
                   'json':{'ok':True,'result':{'card':{'handle':'loop-audit','board_ref':'board:'+self.manifest.anx.board}}}}

    def drift(self, every):
        self.respond(self.card,{'tool':'anx','contains':['context','card:loop-audit'],'json':{'ok':True,'result':{'work':{'cadence':
            {'every_seconds':every,'progress_budget_seconds':1800,'stop_rule':'STOP file present'}}}}})
        with patch.object(lifecycle,'installed_jobs',return_value={}), patch.object(lifecycle.hooks,'drift',return_value=[]):
            return [d for d in lifecycle.installation_drift(self.manifest) if 'cadence' in d]

    def test_cadence_read_from_context(self):
        self.assertEqual(self.drift(1800),[])
        self.assertEqual(self.drift(3600),['loop card cadence drift card:loop-audit'])


class BridgePlanTests(FakeCase):
    def plan(self, running):
        self.manifest.launchd.path=[str(Path.home()/'.local/bin'),'/usr/bin','/bin']
        self.manifest.anx.bridge_answers=True
        self.respond({'tool':'anx','contains':['bridge','status'],'json':{'ok':True,'result':{'running':running}}})
        out=io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(io.StringIO()):
            lifecycle.up(self.manifest)
        return [l for l in out.getvalue().splitlines() if l.startswith('UP_ACTION') and 'bridge' in l]

    def test_running_host_bridge_is_adopted_not_reinstalled(self):
        self.assertEqual([l.split()[-1] for l in self.plan(True)],['add'])
        self.assertEqual([l.split()[-1] for l in self.plan(False)],['install','add'])

    def test_bridge_off_by_default(self):
        self.assertFalse(load(self.file).anx.bridge_answers)
