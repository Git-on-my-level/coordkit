"""Local registration, private identity, bounded model reads and label lifetime."""
import contextlib
import io
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from coordkit import addressed, bus, deliver, mcp_server, routing, usage
from coordkit.manifest import ManifestError, UsageLane, load
from coordkit.state import name_key, read_json, write_json
from tests.test_addressed import AddressedCase, ts


class AutoRegisterTests(AddressedCase):
    def invoke(self, *args, code=0):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                result = bus.main(list(args), self.manifest) or 0
            except SystemExit as exc:
                result = exc.code
        self.assertEqual(result, code)

    def register(self):
        return read_json(routing.home(self.manifest)/'register.json', {})

    def roster(self, **args):
        return {r['name']: r for r in mcp_server.agent_rows(self.manifest, self.rows(), args, time.time())['agents']}

    def test_post_registers_and_becomes_listening_without_bus_identity(self):
        with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.root/'account'), 'CLAUDE_CODE_SESSION_ID': 's1'}):
            self.invoke('post', '--from', 'fresh', '--to', 'all', 'hello')
        entry = self.register()['fresh']
        self.assertEqual(entry['registered_by'], 'post')
        self.assertEqual(entry['identity']['session_id'], 's1')
        row = self.roster()['fresh']
        self.assertEqual(row['status'], 'listening')
        self.assertIn('posted', row['why'])
        self.assertEqual(row['account'], 'account')
        self.assertNotIn('identity', self.rows()[0])
        self.assertNotIn(str(self.root/'account'), self.manifest.paths.bus_log.read_text())

    def test_skip_internal_and_invalid_names(self):
        for name in ('lead', 'coordkit', 'service-agent', 'coordkit-report', 'all', '../bad'):
            routing.write_register(self.manifest, name, 'post')
        self.assertEqual(self.register(), {})

    def test_mcp_sender_activity_is_not_listening_evidence(self):
        # Include an old registration: service posts must not revive it either.
        for name in (self.manifest.mcp.identity, 'custom-mcp'):
            with self.subTest(name=name):
                self.manifest.mcp.identity = name
                write_json(routing.home(self.manifest)/'register.json', {name: {}})
                self.invoke('post', '--from', name, '--to', 'all', 'service update')
                self.assertFalse(addressed.listening(self.manifest, name)[0])
                self.assertFalse(self.roster()[name]['listening'])

    def test_legacy_manifest_metadata_survives_registration(self):
        self.invoke('post', '--from', 'worker', '--to', 'all', 'hello')
        entry = self.register()['worker']
        # Even an old runtime label cannot override permanent manifest metadata.
        write_json(routing.home(self.manifest)/'register.json', {
            'worker': dict(entry, label='runtime label', description='runtime description', registered_at=1)})
        row = self.roster()['worker']
        self.assertEqual((row['label'], row['description']), ('worker', 'ck-sample-worker'))
        self.assertEqual(row['label_source'], 'manifest')
        self.assertFalse(row['label_expired'])

    def delayed_identity_write(self, writer):
        """A writer delayed at lock entry must select the current session inside it."""
        import threading
        from concurrent.futures import ThreadPoolExecutor
        routing.write_register(self.manifest, 'fresh', 'post', {'session_id': 'initial'})
        entries = self.register()
        entries['fresh']['identity']['refreshed_at'] = 1
        write_json(routing.home(self.manifest)/'register.json', entries)
        note = deliver.note_path(self.manifest, 'fresh')
        write_json(note, {'name': 'fresh', 'session_id': 'old'})
        waiting, resume = threading.Event(), threading.Event()
        main_thread = threading.get_ident()
        original_lock = routing.locked

        @contextlib.contextmanager
        def delayed_lock(path, *args, **kwargs):
            if path == routing.home(self.manifest)/'register.lock' and threading.get_ident() != main_thread:
                waiting.set()
                if not resume.wait(5):
                    raise TimeoutError('test did not release delayed identity writer')
            with original_lock(path, *args, **kwargs) as stream:
                yield stream

        with patch.object(routing, 'locked', delayed_lock), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(writer, self.manifest, 'fresh')
            try:
                self.assertTrue(waiting.wait(5), 'identity writer did not reach the lock')
                write_json(note, {'name': 'fresh', 'session_id': 'new'})
                routing.write_register(self.manifest, 'fresh', 'post')
                self.assertEqual(self.register()['fresh']['identity']['session_id'], 'new')
            finally:
                resume.set()
            future.result(timeout=5)
        self.assertEqual(self.register()['fresh']['identity']['session_id'], 'new')

    def test_delayed_registration_cannot_replace_newer_identity(self):
        self.delayed_identity_write(lambda manifest, name: routing.write_register(manifest, name, 'post'))

    def test_delayed_hook_refresh_cannot_replace_newer_identity(self):
        self.delayed_identity_write(routing.refresh_identity)

    def test_refresh_preserves_registration_policy_and_filters_identity(self):
        routing.write_register(self.manifest, 'fresh', 'post', {'session_id': 's1', 'TOKEN': 'secret'})
        original = self.register()['fresh']
        path = routing.home(self.manifest)/'register.json'
        write_json(path, {'fresh': dict(original, role='review', fallback='lead-a')})
        routing.write_register(self.manifest, 'fresh', 'ack', {'session_id': 's2', 'arbitrary': 'secret'})
        updated = self.register()['fresh']
        self.assertEqual(updated['registered_at'], original['registered_at'])
        self.assertEqual(updated['registered_by'], 'post')
        self.assertEqual((updated['role'], updated['fallback']), ('review', 'lead-a'))
        self.assertEqual(set(updated['identity']), {'session_id', 'refreshed_at'})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('secret', path.read_text())

    def test_wait_registers_at_arming(self):
        self.invoke('wait', '--to', 'fresh', '--timeout', '0', code=3)
        self.assertEqual(self.register()['fresh']['registered_by'], 'wait')
        self.assertEqual(addressed.listening(self.manifest, 'fresh')[1], 'fresh bus wait cursor')

    def test_both_ack_paths_register(self):
        for kind in ('handoff', 'ask', 'ruling'):
            self.incumbent_row(kind, 'lead-a', 'all', kind, owner='ack-'+kind)
            self.invoke('ack', kind, '--from', 'ack-'+kind)
            self.assertEqual(self.register()['ack-'+kind]['registered_by'], 'ack')

    def test_failed_post_or_ack_does_not_register(self):
        self.invoke('ack', 'absent', '--from', 'fresh', code=2)
        self.invoke('post', '--from', 'fresh', '--to', 'absent', 'hello', code=2)
        self.assertEqual(self.register(), {})

    def test_relay_append_does_not_register_or_prove_local_liveness(self):
        bus.append([dict(id='remote', ts=ts(time.time()), frm='remote', to='all', kind='msg', text='hello', origin='relay:other')], project=False)
        self.assertEqual(self.register(), {})
        self.assertFalse(addressed.listening(self.manifest, 'remote')[0])

    def test_question_adapter_rows_do_not_manufacture_listening_evidence(self):
        self.incumbent_row('synthetic', 'worker-ui', 'lead-a', 'ask', question='q-blocked', ago=1)
        self.assertFalse(addressed.listening(self.manifest, 'worker-ui')[0])

    def test_evidence_priority_and_expiration(self):
        self.incumbent_row('post', 'fresh', 'all', 'msg', ago=10)
        cursor = self.manifest.paths.bus_log.parent/'state/cursors'/f'{name_key("fresh")}.wait'
        cursor.parent.mkdir(parents=True, exist_ok=True)
        cursor.write_text('0')
        with routing.locked(cursor.with_suffix('.wait.lock')):
            self.assertEqual(addressed.listening(self.manifest, 'fresh')[1], 'armed bus wait lock')
            write_json(deliver.note_path(self.manifest, 'fresh'), {'name': 'fresh', 'at': time.time()})
            self.assertIn('hook delivery', addressed.listening(self.manifest, 'fresh')[1])
        deliver.note_path(self.manifest, 'fresh').unlink()
        self.assertEqual(addressed.listening(self.manifest, 'fresh')[1], 'fresh bus wait cursor')
        old = time.time() - 1000
        os.utime(cursor, (old, old))
        self.assertIn('posted', addressed.listening(self.manifest, 'fresh')[1])
        self.assertFalse(addressed.listening(self.manifest, 'fresh', at=time.time()+901)[0])

    def test_identity_priority_and_no_environment_passthrough(self):
        from coordkit.identity import home
        write_json(home(self.manifest)/'sessions.json', {'bound': {'name': 'fresh', 'config_dir': '/binding'}})
        env = {'CLAUDE_CONFIG_DIR': '/env', 'CLAUDE_CODE_SESSION_ID': 'env', 'API_TOKEN': 'secret'}
        self.assertEqual(routing.identity_sources(self.manifest, 'fresh', env)['config_dir'], '/env')
        write_json(deliver.note_path(self.manifest, 'fresh'), dict(name='fresh', session_id='hook', transcript_path='/hook/projects/p/hook.jsonl', config_dir='/hook', permission_mode='default'))
        self.assertEqual(routing.identity_sources(self.manifest, 'fresh', env)['session_id'], 'hook')
        self.manifest.lanes.append(UsageLane('fresh', '/lane', '/cwd', 'opus', 'high', session_id='lane'))
        result = routing.identity_sources(self.manifest, 'fresh', env)
        self.assertEqual((result['config_dir'], result['session_id']), ('/lane', 'lane'))
        self.assertEqual(result['transcript_path'], '/lane/projects/-cwd/lane.jsonl')
        self.assertLessEqual(set(result), routing.IDENTITY_KEYS)
        self.assertNotIn('secret', json.dumps(result))

    def test_hook_permission_capture_throttling_and_manifest_policy(self):
        self.manifest.paths.bus_log.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.paths.bus_log.touch()
        payload = dict(hook_event_name='PostToolUse', session_id='s1', permission_mode='acceptEdits')
        with patch.dict(os.environ, {'COORDKIT_AS': 'lead-a', 'CLAUDE_CONFIG_DIR': str(self.root/'account')}):
            deliver.deliver(self.manifest, payload, io.StringIO())
            entry = self.register()['lead-a']
            self.assertEqual(entry['identity']['permission_mode'], 'acceptEdits')
            self.assertNotIn('registered_at', entry)
            self.assertEqual(routing.registry(self.manifest)['lead-a']['source'], 'manifest')
            with patch.object(routing, 'probe_identity') as probe:
                deliver.deliver(self.manifest, payload, io.StringIO())
                probe.assert_not_called()
                payload['permission_mode'] = 'default'
                deliver.deliver(self.manifest, payload, io.StringIO())
                probe.assert_not_called()
        self.assertEqual(read_json(deliver.note_path(self.manifest, 'lead-a'))['permission_mode'], 'default')

    def test_optional_identity_failure_does_not_replay_flushed_delivery(self):
        self.incumbent_row('anchor', 'sender', 'lead-a')
        stat = self.manifest.paths.bus_log.stat()
        write_json(deliver.cursor_path(self.manifest, 'lead-a'), {'file': [stat.st_dev, stat.st_ino], 'offset': 0})
        payload = dict(hook_event_name='PostToolUse', session_id='s1')
        with patch.dict(os.environ, {'COORDKIT_AS': 'lead-a'}):
            with patch.object(routing, 'refresh_identity', side_effect=OSError('optional metadata failed')):
                output = io.StringIO()
                self.assertEqual(deliver.run_hook(self.manifest, json.dumps(payload), output), 0)
                self.assertIn('anchor', output.getvalue())
            output = io.StringIO()
            deliver.run_hook(self.manifest, json.dumps(payload), output)
            self.assertEqual(output.getvalue(), '')

    def test_pid_probe_is_best_effort_and_reuse_is_rejected(self):
        from coordkit.adapters import claude_resume
        (self.root/'s.jsonl').touch()
        record = {'transcript_path': str(self.root/'s.jsonl')}
        with patch.object(claude_resume, 'lsof_binary', return_value='lsof'), patch.object(claude_resume, 'probe', return_value=SimpleNamespace(returncode=0, stdout='42\n', stderr='')), patch.object(claude_resume, 'start_time', return_value='Monday'):
            result = routing.probe_identity(record)
            self.assertEqual(routing.identity_pid(result), 42)
        with patch.object(claude_resume, 'start_time', return_value='Tuesday'):
            self.assertIsNone(routing.identity_pid(result))
        with patch.object(claude_resume, 'lsof_binary', side_effect=OSError):
            self.assertNotIn('pid', routing.probe_identity(record))

    def test_lane_display_is_not_routing_registration(self):
        self.manifest.lanes.append(UsageLane('unseen-lane', str(self.root/'account'), '/cwd', 'opus', 'high'))
        self.assertEqual(self.roster()['unseen-lane']['config_dir'], str(self.root/'account'))
        self.assertNotIn('unseen-lane', addressed.known_names(self.manifest))
        self.assertFalse((routing.home(self.manifest)/'register.json').exists())

    def test_projection_clips_and_observes_drift(self):
        lane = UsageLane('model-lane', str(self.root/'account'), '/cwd', 'opus', 'high', session_id='s1')
        self.manifest.lanes.append(lane)
        path = usage.transcript_dir(lane)/'s1.jsonl'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'type': 'assistant', 'timestamp': '2026-11-09T00:00:00Z', 'message': {'model': 'claude-sonnet-5-5'}})+'\n')
        row = self.roster()['model-lane']
        self.assertEqual((row['model'], row['model_launch'], row['model_drift']), ('claude-sonnet-5-5', 'opus', True))
        self.assertEqual(row['model_observed_at'], '2026-11-09T00:00:00Z')
        routing.write_register(self.manifest, 'fresh', 'post', {'session_id': 's'*5000})
        self.assertEqual(len(self.roster()['fresh']['session_id']), 4096)

    def test_labels_expire_except_manifest_and_new_state_replaces(self):
        self.incumbent_row('old', 'fresh', 'all', 'state', 'old label', ago=25*3600)
        routing.write_register(self.manifest, 'fresh', 'post', {})
        row = self.roster()['fresh']
        self.assertIsNone(row['label'])
        self.assertTrue(row['label_expired'])
        self.assertEqual(row['last_state'], 'old label')
        self.incumbent_row('new', 'fresh', 'all', 'state', 'new label', ago=10)
        self.assertEqual(self.roster()['fresh']['label'], 'new label')
        self.assertFalse(self.roster()['fresh']['label_expired'])
        self.manifest.addressed.agents['fresh'] = {'label': 'permanent', 'registered_at': 1}
        self.assertEqual(self.roster()['fresh']['label'], 'permanent')
        self.assertFalse(self.roster()['fresh']['label_expired'])

    def test_saved_post_identity_outranks_older_session_binding_on_reads(self):
        from coordkit.identity import home
        write_json(home(self.manifest)/'sessions.json', {'old': {'name': 'fresh', 'config_dir': '/old'}})
        routing.write_register(self.manifest, 'fresh', 'post', {'session_id': 'new', 'config_dir': '/new'})
        self.assertEqual(self.roster()['fresh']['session_id'], 'new')
        self.assertEqual(self.roster()['fresh']['config_dir'], '/new')
        write_json(deliver.note_path(self.manifest, 'fresh'), {'name': 'fresh', 'session_id': 'hook', 'transcript_path': '/hook/projects/p/hook.jsonl'})
        self.assertEqual(self.roster()['fresh']['session_id'], 'hook')
        self.assertEqual(self.roster()['fresh']['config_dir'], '/hook')

    def test_changed_transcript_binding_discards_old_pid(self):
        self.manifest.lanes.append(UsageLane('fresh', '/new', '/cwd', 'opus', 'high', session_id='same'))
        old = dict(config_dir='/old', session_id='same', transcript_path='/old/projects/p/same.jsonl',
                   pid=42, pid_started='Monday')
        result = routing.identity_sources(self.manifest, 'fresh', env={}, stored=old)
        self.assertNotIn('pid', result)
        self.assertEqual(result['transcript_path'], '/new/projects/-cwd/same.jsonl')

    def test_concurrent_registrations_preserve_every_sender(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda n: routing.write_register(self.manifest, n, 'post', {}),
                          ['worker-'+str(i) for i in range(12)]))
        self.assertEqual(len(self.register()), 12)

    def test_duration_validation(self):
        for key in ('activity_ttl', 'label_ttl'):
            self.file.write_text(self.file.read_text().replace('[addressed]\n', f'[addressed]\n{key} = "invalid"\n'))
            with self.assertRaises(ManifestError):
                load(self.file)
            self.file.write_text(self.file.read_text().replace(f'{key} = "invalid"\n', ''))

    def test_observed_model_tail_is_bounded_and_family_aliases(self):
        path = self.root/'large.jsonl'
        assistant = dict(type='assistant', timestamp='2026-11-09T00:00:00Z', message={'model': 'claude-opus-4-5'})
        path.write_text(json.dumps(assistant)+'\n'+('x'*usage.MODEL_TAIL_BYTES)+'\n')
        self.assertEqual(usage.observed_model([path]), (None, None))
        with path.open('a') as stream:
            stream.write(json.dumps(assistant)+'\n'+json.dumps({'type': 'user'})+'\n')
        model, at = usage.observed_model([path])
        self.assertEqual(model, 'claude-opus-4-5')
        self.assertIsNotNone(at)
        self.assertTrue(usage.model_matches('opus', model))
        self.assertFalse(usage.model_matches('sonnet', model))
        self.assertFalse(usage.model_matches('opus-4-1', model))
