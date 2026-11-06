import contextlib
import io
import json
import time
from unittest.mock import patch, Mock
from coordkit import bus, cli, hooks, deliver, wake
from coordkit.adapters.builtin import Agentctl
from coordkit.manifest import ManifestError, load
from coordkit.process import ToolError
from coordkit.state import read_json, write_json
from tests.support import FakeCase


class WakeTests(FakeCase):
    def setUp(self):
        super().setUp()
        self.manifest.bus.mirror = False
        bus.configure(self.manifest)
        self.adapter = Mock(name='wake-adapter')
        self.adapter.name = 'fake'
        self.adapter.wake.return_value = {'status':'woken'}
        for target, value in [('coordkit.wake.wake_adapters', [self.adapter]),
                              ('coordkit.wake.is_listening', False)]:
            p = patch(target, return_value=value); p.start(); self.addCleanup(p.stop)

    def post(self, to='worker', kind='msg', text='work'):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            bus.main(['post','--from','lead','--to',to,'--kind',kind,'--force',text],self.manifest)

    def test_all_addressed_kinds_wake_burst_once_aliases_resolve(self):
        self.manifest.bus.aliases={'alias':'worker'}
        self.post(to='alias', kind='answer')
        self.post(text='second')
        self.assertEqual(self.adapter.wake.call_count,1)
        record=read_json(self.manifest.paths.bus_log.parent/'state/wakes/worker.json')
        self.assertEqual(record['status'],'woken')
        self.assertIn('-wake-worker-',record['key'])
        self.assertTrue((self.manifest.paths.bus_log.parent/'state/wakes/attempts.jsonl').exists())

    def test_broadcast_fyi_listening_and_retirement_never_wake(self):
        self.post(to='all'); self.post(kind='fyi')
        with patch('coordkit.wake.is_listening',return_value=True): self.post()
        self.adapter.wake.assert_not_called()
        with contextlib.redirect_stdout(io.StringIO()): wake.change(self.manifest,'worker',owner='lead')
        with self.assertRaisesRegex(ManifestError,'redirect to lead'): self.post()
        with contextlib.redirect_stdout(io.StringIO()): wake.change(self.manifest,'worker',undo=True)
        self.post(); self.adapter.wake.assert_called_once()

    def test_failures_debounced_and_pending_is_recorded_before_call(self):
        path=self.manifest.paths.bus_log.parent/'state/wakes/worker.json'
        def fail(*args):
            self.assertEqual(read_json(path)['attempts'][0]['status'],'pending')
            raise RuntimeError('offline')
        self.adapter.wake.side_effect=fail
        self.post(); self.post(text='more')
        self.assertEqual(read_json(path)['status'],'failed')
        self.assertEqual(self.adapter.wake.call_count,1)
        with patch('coordkit.wake.time', create=True): pass

    def fake_agentctl(self, body):
        script=self.root/'fake-agentctl'
        script.write_text('#!/bin/sh\n'+body+'\n'); script.chmod(0o755)
        client=Mock(binary=str(script))
        client.recent.return_value=[dict(id='old',created_at='2000',labels=['worker']),dict(id='latest',created_at='2001',labels=['worker'])]
        wake.wake_path(self.manifest,'worker').parent.mkdir(parents=True,exist_ok=True)
        return client

    def test_agentctl_latest_execution_and_request_key(self):
        adapter=Agentctl(); seen=self.root/'seen'
        client=self.fake_agentctl(f'echo "$@" > {seen}; cat >> {seen}')
        with patch.object(adapter,'client',return_value=client):
            self.assertEqual(adapter.wake(self.manifest,'worker',[],'request')['execution'],'latest')
        text=seen.read_text()
        self.assertTrue(text.startswith('continue latest --request-key request'))
        self.assertIn('whole bus inbox',text)

    def test_agentctl_continuation_outlives_the_wake_call(self):
        # The resumed turn belongs to agentctl's own process; wake must not bound or kill it.
        adapter=Agentctl(); done=self.root/'done'
        client=self.fake_agentctl(f'cat >/dev/null; sleep 6; touch {done}')
        started=time.time()
        with patch.object(adapter,'client',return_value=client):
            result=adapter.wake(self.manifest,'worker',[],'request')
        self.assertEqual(result['status'],'woken')
        self.assertLess(time.time()-started,6)
        deadline=time.time()+10
        while not done.exists() and time.time()<deadline:
            time.sleep(0.2)
        self.assertTrue(done.exists())

    def test_agentctl_immediate_refusal_is_a_failed_wake(self):
        adapter=Agentctl()
        client=self.fake_agentctl('cat >/dev/null; echo refused; exit 8')
        with patch.object(adapter,'client',return_value=client), self.assertRaises(ToolError):
            adapter.wake(self.manifest,'worker',[],'request')

    def test_minimal_manifest_and_optional_adapters(self):
        self.file.write_text('[project]\nname="sample"\ngit_repo="owner/repo"\n')
        cfg=load(self.file)
        self.assertFalse(cfg.anx.workspace)
        self.assertEqual(cfg.bus.wake_debounce_minutes,5)
        with patch('coordkit.adapters.shutil.which',return_value=None):
            from coordkit.adapters import available
            self.assertNotIn('agentctl',{a.name for a in available(cfg)})


class MalformedWakeRecordTests(FakeCase):
    def check_invalid(self, content):
        path = wake.wake_path(self.manifest, 'worker')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        adapter = Mock(spec=['name', 'wake'])
        adapter.name = 'fake'
        adapter.wake.return_value = {'status': 'woken'}
        row = dict(id='work', frm='lead', to='worker', kind='ask')
        error = io.StringIO()
        with patch.object(wake, '_WARNED_RECORDS', set()), \
                patch.object(wake, 'wake_adapters', return_value=[adapter]), \
                patch('coordkit.addressed.listening', return_value=(False, 'idle')), \
                patch('coordkit.wake.time.time', return_value=10000), \
                contextlib.redirect_stderr(error):
            self.assertFalse(wake.recently_woken(self.manifest, 'worker'))
            self.assertFalse(wake.recently_woken(self.manifest, 'worker'))
            wake.notify(self.manifest, [row])
            wake.notify(self.manifest, [dict(row, id='second')])
        self.assertEqual(error.getvalue().count('WAKE_INVALID_RECORD'), 1)
        adapter.wake.assert_called_once()
        record = read_json(path)
        self.assertEqual(record['at'], 10000)
        self.assertEqual(record['status'], 'woken')
        self.assertNotEqual(record['key'], 'old-key')
        self.assertEqual(record['attempts'], [{'adapter': 'fake', 'status': 'woken'}])

    def test_missing_at_or_key_is_absent_warns_once_and_is_replaced(self):
        records = ({'status': 'woken', 'key': 'old-key'},
                   {'status': 'pending', 'at': 10000})
        for record in records:
            with self.subTest(record=record):
                self.check_invalid(json.dumps(record))

    def test_invalid_json_shape_or_field_types_do_not_block_wake(self):
        contents = ['broken', 'null', '[]', '{}']
        base = {'at': 10000, 'key': 'old-key', 'status': 'pending'}
        contents += [json.dumps(dict(base, **change)) for change in
                     ({'at': 'recent'}, {'at': None}, {'at': True}, {'at': float('nan')},
                      {'at': float('inf')}, {'key': None}, {'key': ''}, {'attempts': {}},
                      {'attempts': [{}]}, {'attempts': ['x']}, {'attempts': [{'status': 1}]})]
        for content in contents:
            with self.subTest(content=content):
                self.check_invalid(content)


class HookManagementTests(FakeCase):
    def test_install_reconcile_preserve_edits_uninstall(self):
        target=self.root/'settings.json'; original={'theme':'dark'}; write_json(target,original)
        with patch('coordkit.paths.install_root',return_value=self.root/'old'):
            deliver.install(self.manifest,target)
            deliver.install(self.manifest,target)
        record=read_json(deliver.receipt_path(self.manifest,target))
        self.assertEqual(record['before'],original)
        changed=read_json(target); changed['theme']='light'; write_json(target,changed)
        with patch('coordkit.paths.install_root',return_value=self.root/'new'):
            self.assertEqual(hooks.reconcile(self.manifest),[str(target)])
        self.assertEqual(read_json(target)['theme'],'light')
        self.assertIn('COORDKIT_HOOK_OWNER=',target.read_text())
        deliver.uninstall(self.manifest,target)
        self.assertEqual(read_json(target)['theme'],'light')
        self.assertFalse(hooks.entries(read_json(target)))

    def test_user_edit_to_owned_command_refused(self):
        target=self.root/'settings.json'; write_json(target,{})
        deliver.install(self.manifest,target)
        doc=read_json(target); doc['hooks']['PostToolUse'][0]['hooks'][0]['command']+=' edited'; write_json(target,doc)
        before=target.read_bytes()
        with self.assertRaisesRegex(ManifestError,'hook_rollback_conflict'): hooks.reconcile(self.manifest)
        self.assertEqual(target.read_bytes(),before)

class DeliveryProjectionTests(FakeCase):
    def test_hook_paths_and_roster_match_full_manifest(self):
        from coordkit.adapters.claude_hook import load as hook_load
        from coordkit import routing
        self.file.write_text(self.file.read_text()+'\n[paths]\nbus_log="shared/bus.jsonl"\n[addressed.agents.reader]\nrole="workers"\n[addressed.roles]\nworkers=["reader"]\n')
        full, projection=load(self.file), hook_load(self.file)
        self.assertEqual(full.paths.bus_log,projection.paths.bus_log)
        self.assertEqual(full.paths.root,projection.paths.root)
        self.assertEqual(routing.roles(full),routing.roles(projection))
        self.file.write_text('mode="shadow"\n'+self.file.read_text())
        with self.assertRaisesRegex(ManifestError,'hook_not_live'): hook_load(self.file)

class HookCrashRecoveryTests(FakeCase):
    def test_pending_migration_recovers_and_keeps_unrelated_hook(self):
        target=self.root/'settings.json'; write_json(target,{})
        with patch('coordkit.paths.install_root',return_value=self.root/'old'):
            deliver.install(self.manifest,target)
        current=read_json(target)
        current['hooks']['Stop']=[{'hooks':[{'type':'command','command':'user-owned'}]}]
        write_json(target,current)
        receipt=deliver.receipt_path(self.manifest,target)
        actual_write=hooks.write_json
        def fail_final(path,doc):
            if path==receipt and 'migrations' in doc and 'pending_after' not in doc:
                raise OSError('simulated crash after settings write')
            actual_write(path,doc)
        with patch('coordkit.paths.install_root',return_value=self.root/'new'):
            with patch.object(hooks,'write_json',side_effect=fail_final),self.assertRaises(OSError):
                hooks.reconcile(self.manifest)
            self.assertIn('pending_after',read_json(receipt))
            hooks.reconcile(self.manifest)
        self.assertNotIn('pending_after',read_json(receipt))
        deliver.uninstall(self.manifest,target)
        self.assertEqual(read_json(target)['hooks']['Stop'][0]['hooks'][0]['command'],'user-owned')

class WakeChainTests(FakeCase):
    def setUp(self):
        super().setUp()
        self.manifest.bus.mirror = False
        self.manifest.addressed.agents = {'lane-a': {}, 'lane-b': {}}
        self.manifest.bus.aliases = {'alias-a': 'lane-a', 'alias-b': 'lane-b'}
        self.adapter = Mock(spec=['name', 'wake', 'listening'])
        self.adapter.name = 'fake'
        self.adapter.wake.return_value = {'status': 'woken'}
        self.adapter.listening.return_value = False
        self.adapters_patch = patch('coordkit.wake.wake_adapters', return_value=[self.adapter])
        self.adapters_patch.start(); self.addCleanup(self.adapters_patch.stop)
        listener = patch('coordkit.addressed.listening', return_value=(False, 'idle'))
        listener.start(); self.addCleanup(listener.stop)
        self.at = 10000

    def send(self, sender, recipient, kind='msg', reply=None):
        row = dict(id='test', frm=sender, to=recipient, kind=kind)
        if reply:
            row['re'] = reply
        with patch('coordkit.wake.time.time', return_value=self.at):
            wake.notify(self.manifest, [row])

    def test_self_posts_never_wake_after_alias_resolution(self):
        self.send('lane-a', 'alias-a')
        self.send('alias-b', 'lane-b', kind='ask')
        self.adapter.wake.assert_not_called()

    def test_active_execution_and_recent_success_are_listening(self):
        self.adapter.listening.return_value = True
        self.send('lane-a', 'lane-b', kind='ask')
        self.adapter.wake.assert_not_called()
        self.adapter.listening.return_value = False
        self.send('lane-a', 'lane-b', kind='ask')
        with patch('coordkit.wake.time.time', return_value=self.at + 1):
            self.assertTrue(wake.is_listening(self.manifest, 'lane-b', []))
        self.assertEqual(self.adapter.wake.call_count, 1)

    def test_two_lane_ping_pong_only_idle_followup_ask_wakes(self):
        self.send('lane-a', 'alias-b', kind='ask')
        self.assertEqual(self.adapter.wake.call_count, 1)
        self.send('lane-b', 'alias-a', kind='answer', reply='ask-a')
        self.send('alias-b', 'lane-a', kind='ack', reply='ask-a')
        self.send('lane-b', 'lane-a', kind='answer')  # woken sender, even without --re
        self.assertEqual(self.adapter.wake.call_count, 1)
        self.adapter.listening.return_value = True
        self.send('lane-b', 'lane-a', kind='ruling', reply='ask-a')
        self.assertEqual(self.adapter.wake.call_count, 1)
        self.adapter.listening.return_value = False
        self.send('lane-b', 'lane-a', kind='ask', reply='ask-a')
        self.assertEqual(self.adapter.wake.call_count, 2)
        # Even after lane-b's wake window expires, an answer cannot restart it.
        self.at += 301
        self.send('lane-a', 'lane-b', kind='answer', reply='ask-b')
        self.assertEqual(self.adapter.wake.call_count, 2)

    def test_woken_lane_can_handoff_unrelated_work(self):
        self.manifest.addressed.agents['lane-c'] = {}
        self.send('lane-a', 'lane-b', kind='ask')
        self.send('lane-b', 'lane-c', kind='handoff')
        self.assertEqual(self.adapter.wake.call_count, 2)
        self.assertEqual(self.adapter.wake.call_args.args[1], 'lane-c')

    def test_referenced_work_kinds_still_wake_an_idle_lane(self):
        kinds = ('handoff', 'ask', 'ruling', 'fail', 'apply_request', 'blocker', 'decision')
        for index, kind in enumerate(kinds, 1):
            with self.subTest(kind=kind):
                self.send('lane-a', 'lane-b', kind=kind, reply='prior-context')
                self.assertEqual(self.adapter.wake.call_count, index)
                self.at += 301

    def test_referenced_reply_answer_and_ack_feedback_stay_quiet(self):
        for kind in ('reply', 'answer', 'ack'):
            with self.subTest(kind=kind):
                self.send('lane-a', 'lane-b', kind=kind, reply='prior-context')
        self.adapter.wake.assert_not_called()

    def test_agentctl_active_execution_prevents_continue(self):
        adapter = Agentctl()
        client = Mock(binary='fake-agentctl')
        client.recent.return_value = [dict(id='running', state='running', labels=['lane-a'])]
        with patch.object(adapter, 'client', return_value=client), patch('coordkit.adapters.builtin.run') as run:
            self.assertTrue(adapter.listening(self.manifest, 'lane-a', []))
            self.assertEqual(adapter.wake(self.manifest, 'lane-a', [], 'key')['status'], 'listening')
        run.assert_not_called()

class WakeIdempotencyTests(WakeChainTests):
    def test_crashed_pending_attempt_reuses_key_after_debounce(self):
        executions = {}
        first = True
        def resume(manifest, name, rows, key):
            nonlocal first
            executions.setdefault(key, 'continued-once')
            if first:
                first = False
                raise KeyboardInterrupt('lost response after continuation was launched')
            return {'status': 'woken', 'execution': executions[key]}
        self.adapter.wake.side_effect = resume
        with self.assertRaises(KeyboardInterrupt):
            self.send('lane-a', 'lane-b', kind='ask')
        original = read_json(wake.wake_path(self.manifest, 'lane-b'))
        self.assertEqual(original['status'], 'pending')
        self.send('lane-a', 'lane-b', kind='ask')
        self.assertEqual(self.adapter.wake.call_count, 1)
        self.at += 301
        self.send('lane-a', 'lane-b', kind='ask')
        result = read_json(wake.wake_path(self.manifest, 'lane-b'))
        self.assertEqual(result['key'], original['key'])
        self.assertEqual(result['status'], 'woken')
        self.assertEqual(result['attempts'][0]['status'], 'pending')
        self.assertEqual(len(result['attempts']), 2)
        self.assertEqual(len(executions), 1)


class WakeScanTests(FakeCase):
    def append(self, *rows):
        log=self.manifest.paths.bus_log; log.parent.mkdir(parents=True,exist_ok=True)
        with log.open('a') as stream:
            for row in rows:
                stream.write(row if isinstance(row,str) else json.dumps(row)+'\n')

    def scan(self):
        with patch.object(wake,'notify') as notify, contextlib.redirect_stdout(io.StringIO()):
            wake.scan(self.manifest)
        return [r for call in notify.call_args_list for r in call.args[1]]

    def test_first_scan_starts_at_end_then_sees_only_new_complete_rows(self):
        self.append(dict(id='old',frm='lead',to='worker',kind='ask'))
        self.assertEqual(self.scan(),[])
        self.append(dict(id='new',frm='lead',to='a,b',kind='ask'),'not json\n','{"id":"partial"')
        self.assertEqual([(r['id'],r['to']) for r in self.scan()],[('new','a'),('new','b')])
        self.assertEqual(self.scan(),[])
        self.append(',"to":"worker"}\n')
        self.assertEqual([r['id'] for r in self.scan()],['partial'])

    def test_truncated_log_restarts_at_end(self):
        self.append(dict(id='one',to='worker'),dict(id='two',to='worker'))
        self.scan()
        self.manifest.paths.bus_log.write_text('')
        self.assertEqual(self.scan(),[])
        self.append(dict(id='three',to='worker'))
        self.assertEqual([r['id'] for r in self.scan()],['three'])
