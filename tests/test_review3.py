import contextlib
import copy
import io
import json
from unittest.mock import patch

from coordkit import hooks, lifecycle
from coordkit.manifest import ManifestError
from coordkit.state import read_json, write_json
from tests.support import FakeCase


class HookRollbackTests(FakeCase):
    def setUp(self):
        super().setUp()
        self.target = self.root/'settings.json'
        self.backup = self.root/'settings.backup.json'
        self.original = {'theme':'dark','hooks':{
            'UserPromptSubmit':[{'hooks':[{'type':'command','command':'python lead_wait.py pending --brief'},
                                         {'type':'command','command':'heartbeat touch'}]}],
            'PreToolUse':[{'matcher':'AskUserQuestion','hooks':[
                {'type':'command','command':'python lead_wait.py ask-guard'}]}]}}
        self.target.write_text(json.dumps(self.original))
        self.backup.write_bytes(self.target.read_bytes())

    def install(self):
        hooks.install(self.manifest,self.target,self.backup)
        return read_json(self.target)

    def down(self):
        with patch.object(lifecycle,'installed_jobs',return_value=[]),contextlib.redirect_stdout(io.StringIO()):
            return lifecycle.down(self.manifest)

    def test_missing_registry_still_restores_and_verifies_hooks(self):
        self.install()
        registry = self.manifest.paths.root/'lifecycle.json'
        write_json(registry,{'jobs':[],'hook_backups':[]})
        registry.unlink()
        self.assertEqual(self.down(),0)
        self.assertEqual(hooks.entries(read_json(self.target)),hooks.entries(self.original))
        self.assertTrue(read_json(hooks.receipt_path(self.manifest))['restored'])
        self.assertEqual(self.down(),0)

    def test_rollback_preserves_concurrent_unrelated_hook_and_settings_edits(self):
        current = self.install()
        extra = {'type':'command','command':'new unrelated hook'}
        current['theme'] = 'light'
        current['hooks']['PreToolUse'][0]['hooks'].append(extra)
        current['hooks']['Stop'][0]['hooks'].append(extra)
        current['hooks']['UserPromptSubmit'][0]['hooks'].append(extra)
        current['hooks']['PostToolUse'] = [{'matcher':'Edit','hooks':[extra]}]
        write_json(self.target,current)
        self.assertEqual(self.down(),0)
        restored = read_json(self.target)
        expected = copy.deepcopy(self.original)
        expected['theme'] = 'light'
        expected['hooks']['PreToolUse'][0]['hooks'].append(extra)
        expected['hooks']['UserPromptSubmit'][0]['hooks'].append(extra)
        expected['hooks']['Stop'] = [{'hooks':[extra]}]
        expected['hooks']['PostToolUse'] = [{'matcher':'Edit','hooks':[extra]}]
        self.assertEqual(hooks.entries(restored),hooks.entries(expected))
        self.assertEqual(restored['theme'],'light')

    def test_managed_hook_conflict_refuses_down_success_without_overwriting(self):
        current = self.install()
        current['hooks']['Stop'][0]['hooks'][0]['command'] += ' --key other'
        write_json(self.target,current)
        before = self.target.read_bytes()
        with self.assertRaisesRegex(ManifestError,'hook_rollback_conflict'):
            self.down()
        self.assertEqual(self.target.read_bytes(),before)
        self.assertFalse(read_json(hooks.receipt_path(self.manifest))['restored'])

    def test_additional_coordkit_hook_refuses_rollback_success(self):
        current = self.install()
        current['hooks']['PostToolUse'] = [{'hooks':[{
            'type':'command','command':hooks.command(self.manifest,'pending')+' --key other'}]}]
        write_json(self.target,current)
        before = self.target.read_bytes()
        with self.assertRaisesRegex(ManifestError,'coordkit waiter hook remains'):
            self.down()
        self.assertEqual(self.target.read_bytes(),before)

    def test_crash_before_hook_edit_and_after_restore_are_retryable(self):
        def fail_target(path, doc):
            if path == self.target:
                raise OSError('before hook write')
            write_json(path,doc)
        with patch.object(hooks,'write_json',side_effect=fail_target):
            with self.assertRaises(OSError): hooks.install(self.manifest,self.target,self.backup)
        self.assertEqual(read_json(self.target),self.original)
        record = read_json(hooks.receipt_path(self.manifest))
        self.assertFalse(record['restored'])
        self.assertEqual(self.down(),0)
        record['restored'] = False
        write_json(hooks.receipt_path(self.manifest),record)
        write_json(self.target,hooks.reverse(record,record['after']))
        self.assertEqual(self.down(),0)
        self.assertEqual(hooks.entries(read_json(self.target)),hooks.entries(self.original))

    def test_legacy_backup_without_edited_snapshot_fails_closed(self):
        write_json(self.manifest.paths.root/'lifecycle.json',{'jobs':[],
                   'hook_backups':[{'target':str(self.target),'backup':str(self.backup)}]})
        with self.assertRaisesRegex(ManifestError,'hook_rollback_snapshot_missing'):
            self.down()


class InstallationDriftTests(FakeCase):
    def setUp(self):
        super().setUp()
        from pathlib import Path
        self.manifest.launchd.path = [str(Path.home()/'.local/bin'),'/usr/bin','/bin']
        self.manifest.loop.loops = {'audit':{}}
        self.good = [
            {'tool':'anx','contains':['orient'],'json':{'ok':True,'result':{'principal':{'id':'lead'}}}},
            {'tool':'anx','contains':['boards','get'],'json':{'ok':True,'result':{'board':{'id':'board'}}}},
            {'tool':'agentctl','contains':['config','doctor'],'json':{'ok':True,'result':{'valid':True}}}]
        self.respond(*self.good)

    def quiet(self, fn, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            return fn(*args,**kwargs)

    def test_incumbent_hook_alongside_registered_hook_is_reported_everywhere(self):
        from coordkit import reconcile
        target, backup = self.root/'settings.json', self.root/'backup.json'
        original = {'hooks':{
            'UserPromptSubmit':[{'hooks':[{'type':'command','command':'python lead_wait.py pending --brief'}]}],
            'PreToolUse':[{'matcher':'AskUserQuestion','hooks':[{'type':'command','command':'python lead_wait.py ask-guard'}]}]}}
        write_json(target,original); write_json(backup,original)
        hooks.install(self.manifest,target,backup)
        self.assertEqual(lifecycle.installation_drift(self.manifest),[])
        current = read_json(target)
        current['hooks']['UserPromptSubmit'].append(original['hooks']['UserPromptSubmit'][0])
        write_json(target,current)
        before = target.read_bytes()
        self.assertEqual(self.quiet(lifecycle.doctor,self.manifest),1)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(lifecycle.up(self.manifest),1)
        self.assertIn('UP_DIFF incumbent lead hook remains UserPromptSubmit',out.getvalue())
        self.assertEqual(self.quiet(reconcile.reconcile,self.manifest,False,10000),5)
        self.assertTrue(any('ask' in c['args'] and 'incumbent lead hook remains' in ' '.join(c['args']).lower() for c in self.calls()))
        self.assertEqual(target.read_bytes(),before)
        self.assertFalse(any('bridge' in c['args'] for c in self.calls()))

    def test_card_cadence_drift_is_reported_everywhere(self):
        from coordkit import reconcile
        self.respond({'tool':'anx','contains':['cards','get','card:loop-audit'],
            'json':{'ok':True,'result':{'card':{'handle':'loop-audit','board_ref':'board:ck-sample',
                'cadence':{'every_seconds':999,'progress_budget_seconds':1800,'stop_rule':'STOP file present'}}}}},*self.good)
        self.assertEqual(self.quiet(lifecycle.doctor,self.manifest),1)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(lifecycle.up(self.manifest),1)
        self.assertIn('UP_DIFF loop card cadence drift card:loop-audit',out.getvalue())
        self.assertEqual(self.quiet(reconcile.reconcile,self.manifest,False,10000),5)
        self.assertTrue(any('ask' in c['args'] and 'cadence drift' in ' '.join(c['args']).lower() for c in self.calls()))
        self.assertFalse(any('cadence' in c['args'] or 'move' in c['args'] for c in self.calls()))

    def test_loop_card_contract_checks_missing_board_budget_stop_and_overrides(self):
        card = {'handle':'loop-audit','board_ref':'board:ck-sample','cadence':{
            'every_seconds':1800,'progress_budget_seconds':1800,'stop_rule':'STOP file present'}}
        for name,change,detail in [
            ('board',lambda c:c.update(board_ref='board:wrong'),'board drift'),
            ('budget',lambda c:c['cadence'].update(progress_budget_seconds=1),'cadence drift'),
            ('stop_rule',lambda c:c['cadence'].update(stop_rule='different'),'cadence drift'),
            ('resolved',lambda c:c.update(resolution='done'),'stop state drift')]:
            with self.subTest(name=name):
                changed = copy.deepcopy(card); change(changed)
                self.respond({'tool':'anx','contains':['cards','get','card:loop-audit'],
                              'json':{'ok':True,'result':{'card':changed}}})
                self.assertTrue(any(detail in d for d in lifecycle.installation_drift(self.manifest)))
        self.respond({'tool':'anx','contains':['cards','get','card:loop-audit'],'exit':2,
                      'json':{'ok':False,'error':{'code':'not_found'}}})
        self.assertTrue(any('loop card missing' in d for d in lifecycle.installation_drift(self.manifest)))
        self.manifest.loop.loops = {'audit':{'default_cadence':'1h','progress_budget':'2h','stop_rule':'reviewed stop'}}
        card['cadence'] = {'every_seconds':3600,'progress_budget_seconds':7200,'stop_rule':'reviewed stop'}
        self.respond({'tool':'anx','contains':['cards','get','card:loop-audit'],
                      'json':{'ok':True,'result':{'card':card}}})
        self.assertEqual(lifecycle.installation_drift(self.manifest),[])


class LeadHookBoundTests(FakeCase):
    def test_lead_hooks_are_bounded_and_fail_open(self):
        import shlex
        for verb in ('ask-guard', 'pending'):
            argv = shlex.split(hooks.command(self.manifest, verb))
            self.assertEqual(argv[:3], ['perl', '-e', 'alarm 5; exec @ARGV'])  # Reviewer s5 review: no unbounded hook
            self.assertIn('wait', argv); self.assertIn(verb, argv)
        hooks.verify_restored(self.manifest, {'hooks': {}})  # restore verification still parses the bounded form


class LeadHookFreshDirTests(FakeCase):
    """A fresh harness config directory has no incumbent lead hook pair to swap."""
    def test_fresh_dir_gets_coordkit_pair_only_when_asked_and_never_twice(self):
        doc = {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'agent-heartbeat hook'}]}]}, 'model': 'x'}
        with self.assertRaises(ManifestError):
            hooks.transformation(self.manifest, doc)  # the strict default still demands the incumbent pair
        after = hooks.transformation(self.manifest, doc, fresh_ok=True)
        added = set(hooks.entries(after)) - set(hooks.entries(doc))
        self.assertEqual(sorted(json.loads(k)[0] for k in added), ['PreToolUse', 'Stop'])
        self.assertEqual(after['model'], 'x')
        with self.assertRaises(ManifestError):
            hooks.transformation(self.manifest, after, fresh_ok=True)  # never a second copy
        target, backup = self.root/'settings.json', self.root/'backup.json'
        target.write_text(json.dumps(doc)); backup.write_text(json.dumps(doc))
        hooks.install(self.manifest, target, backup, fresh_ok=True)
        hooks.restore(self.manifest)
        self.assertEqual(json.loads(target.read_text()), doc)
