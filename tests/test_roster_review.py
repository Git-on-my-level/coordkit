"""PR review regressions: optional identity must not change delivery semantics."""
import contextlib
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

from coordkit import addressed, bus, deliver, mcp_server, routing, usage
from coordkit.manifest import UsageLane, load
from coordkit.state import read_json, write_json
from tests.test_addressed import AddressedCase


class RosterReviewTests(AddressedCase):
    def invoke(self, *args, code=0):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                result = bus.main(list(args), self.manifest) or 0
            except SystemExit as exc:
                result = exc.code
        self.assertEqual(result, code)

    def register(self):
        return read_json(routing.home(self.manifest)/'register.json', {})

    def roster(self):
        return {r['name']: r for r in mcp_server.agent_rows(self.manifest, self.rows(), {}, time.time())['agents']}

    def connector_post(self, text='service update', sender='review-desk'):
        self.manifest.mcp.post_as = [sender]
        return mcp_server.write_call(self.manifest, 'post', {'as': sender, 'to': 'all', 'kind': 'fyi', 'text': text})

    def transcript(self, lane, session, model, mtime):
        path = usage.transcript_dir(lane)/f'{session}.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'type': 'assistant', 'timestamp': '2026-11-09T00:00:00Z',
                                    'message': {'model': model}})+'\n')
        os.utime(path, (mtime, mtime))
        return path

    def test_connector_post_as_cannot_revive_deaf_lane(self):
        self.assertTrue(self.connector_post()['ok'])
        self.assertIn('review-desk', routing.registry(self.manifest))
        self.assertFalse(addressed.listening(self.manifest, 'review-desk')[0])
        self.invoke('post', '--from', 'sender', '--to', 'review-desk', '--kind', 'ask', 'please review')
        self.assertEqual(bus.LAST_POST[1], 'recipient_deaf')

    def test_manifest_post_saves_identity_without_overriding_policy(self):
        policy = dict(routing.registry(self.manifest)['lead-a'])
        with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.root/'worker-account'), 'CLAUDE_CODE_SESSION_ID': 's1'}):
            self.invoke('post', '--from', 'lead-a', '--to', 'all', 'hello')
        self.assertEqual(self.roster()['lead-a']['session_id'], 's1')
        self.assertEqual(self.roster()['lead-a']['config_dir'], str(self.root/'worker-account'))
        self.assertNotIn('registered_by', self.register()['lead-a'])
        self.assertEqual(routing.registry(self.manifest)['lead-a'], policy)

    def test_manifest_ack_saves_identity(self):
        self.incumbent_row('work', 'sender', 'lead-a', 'ask')
        with patch.dict(os.environ, {'CLAUDE_CODE_SESSION_ID': 'acked-session'}):
            self.invoke('ack', 'work', '--from', 'lead-a')
        self.assertEqual(self.roster()['lead-a']['session_id'], 'acked-session')

    def test_removed_manifest_identity_does_not_become_runtime_registration(self):
        for via in ('post', 'hook'):
            with self.subTest(via=via):
                name = 'manifest-' + via
                self.manifest.addressed.agents[name] = {'fallback': 'lead-a'}
                if via == 'post':
                    self.invoke('post', '--from', name, '--to', 'all', 'hello')
                else:
                    routing.refresh_identity(self.manifest, name)
                self.assertIn(name, self.register())
                self.assertEqual(addressed.target_kind(self.manifest, name), 'name')
                del self.manifest.addressed.agents[name]
                self.assertIsNone(addressed.target_kind(self.manifest, name))
                self.assertNotIn(name, addressed.known_names(self.manifest))
                # Cached identity remains available without granting a route.
                self.assertIn(name, self.register())
                # A later real local activity can explicitly register the name.
                routing.write_register(self.manifest, name, 'wait', env={})
                self.assertEqual(addressed.target_kind(self.manifest, name), 'name')

    def test_runtime_registration_survives_manifest_removal_and_legacy_policy(self):
        routing.write_register(self.manifest, 'runtime', 'post', {'session_id': 's1'})
        self.manifest.addressed.agents['runtime'] = {'fallback': 'lead-a'}
        routing.write_register(self.manifest, 'runtime', 'ack', env={})
        del self.manifest.addressed.agents['runtime']
        self.assertEqual(addressed.target_kind(self.manifest, 'runtime'), 'name')
        entries = self.register()
        entries['legacy'] = {'role': 'review', 'fallback': 'lead-a'}
        write_json(routing.home(self.manifest)/'register.json', entries)
        self.assertEqual(addressed.target_kind(self.manifest, 'legacy'), 'name')
        self.assertIn('legacy', routing.roles(self.manifest)['review'])

    def test_shared_unpinned_models_remain_unknown(self):
        for pinned in (False, True):
            with self.subTest(other_pinned=pinned):
                a = UsageLane('review', str(self.root/'account'), '/cwd', 'opus', 'high')
                b = UsageLane('merge', a.config_dir, a.cwd, 'sonnet', 'high', session_id='merge' if pinned else None)
                self.manifest.lanes[:] = [a, b]
                self.transcript(a, 'review', 'claude-opus-4-5', 10)
                self.transcript(b, 'merge', 'claude-sonnet-4-5', 20)
                self.assertIsNone(self.roster()['review']['model'])
                self.assertIsNone(self.roster()['review']['model_drift'])

    def test_registration_failure_cannot_strand_explicit_id_post(self):
        with patch.object(routing, 'write_register', side_effect=OSError('metadata unavailable')):
            with self.assertLogs('coordkit.routing', level='WARNING'):
                self.invoke('post', '--id', 'durable-ask', '--from', 'fresh', '--to', 'worker-ui', '--kind', 'ask', 'do work')
        self.assertEqual(bus.LAST_POST[0]['id'], 'durable-ask')
        self.assertEqual(bus.LAST_POST[1], 'recipient_deaf')
        self.assertTrue(any(r.get('kind') == 'ask' and r['id'] == 'durable-ask' for r in self.rows()))
        self.invoke('post', '--id', 'durable-ask', '--from', 'fresh', '--to', 'worker-ui', '--kind', 'ask', 'do work')
        self.assertEqual(sum(r['id'] == 'durable-ask' for r in self.rows()), 1)
        self.assertIn('fresh', self.register())

    def test_registration_failure_cannot_fail_any_ack_path(self):
        for kind in ('ask', 'handoff', 'ruling'):
            with self.subTest(kind=kind):
                self.incumbent_row(kind, 'sender', 'fresh', kind, owner='fresh')
                with patch.object(routing, 'write_register', side_effect=ValueError('bad metadata')):
                    with self.assertLogs('coordkit.routing', level='WARNING'):
                        self.invoke('ack', kind, '--from', 'fresh')
                self.assertTrue(any(r.get('kind') == 'ack' and r.get('re') == kind for r in self.rows()))

    def test_registration_failure_cannot_fail_wait(self):
        with patch.object(routing, 'write_register', side_effect=OSError('metadata unavailable')):
            with self.assertLogs('coordkit.routing', level='WARNING'):
                self.invoke('wait', '--to', 'fresh', '--timeout', '0', code=3)
        self.assertTrue(routing.waiter_evidence(self.manifest, 'fresh')[0])

    def test_forged_spill_text_cannot_grant_ref_read(self):
        refs = self.manifest.paths.bus_log.parent/'state'/'refs'
        refs.mkdir(parents=True)
        secret = refs/'unpublished.txt'
        secret.write_text('UNPUBLISHED')
        result = self.connector_post(f'cut … [full: {secret}]')
        self.assertTrue(result['ok'])
        row = next(r for r in self.rows() if r['id'] == result['id'])
        for full in (False, True):
            with self.subTest(full=full), self.assertRaises(ValueError):
                mcp_server.read_ref(self.manifest, row, {'full': full})

    def test_session_model_excludes_newer_subagent(self):
        lane = UsageLane('review', str(self.root/'account'), '/cwd', 'opus', 'high', session_id='parent')
        self.manifest.lanes.append(lane)
        self.transcript(lane, 'parent', 'claude-opus-4-5', 10)
        self.transcript(lane, 'parent/subagents/child', 'claude-haiku-4-5', 20)
        row = self.roster()['review']
        self.assertEqual(row['model'], 'claude-opus-4-5')
        self.assertFalse(row['model_drift'])

    def test_genuine_spill_with_attachment_ignores_forged_text_marker(self):
        refs = self.manifest.paths.bus_log.parent/'state'/'refs'
        refs.mkdir(parents=True)
        secret, attached = refs/'unpublished.txt', refs/'attachment.txt'
        secret.write_text('UNPUBLISHED')
        attached.write_text('attachment')
        text = f'[full: {secret}] ' + 'x' * bus.TEXT_CAP
        self.invoke('post', '--from', 'fresh', '--to', 'all', '--ref', str(attached), text)
        row = bus.LAST_POST[0]
        self.assertEqual(mcp_server.read_ref(self.manifest, row, {})['text'], 'attachment')
        self.assertEqual(mcp_server.read_ref(self.manifest, row, {'full': True})['text'], text+'\n')

    def test_explicit_binding_isolates_model_in_shared_directory(self):
        a = UsageLane('review', str(self.root/'account'), '/cwd', 'opus', 'high')
        b = UsageLane('merge', a.config_dir, a.cwd, 'sonnet', 'high')
        self.manifest.lanes[:] = [a, b]
        parent = self.transcript(a, 'review', 'claude-opus-4-5', 10)
        self.transcript(b, 'merge', 'claude-sonnet-4-5', 20)
        write_json(deliver.note_path(self.manifest, 'review'),
                   {'name': 'review', 'session_id': 'review', 'transcript_path': str(parent)})
        self.assertEqual(self.roster()['review']['model'], 'claude-opus-4-5')

    def test_connector_post_as_does_not_capture_service_environment(self):
        with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.root/'service-account'), 'CLAUDE_CODE_SESSION_ID': 'service-session'}):
            self.assertTrue(self.connector_post()['ok'])
        row = self.roster()['review-desk']
        self.assertIsNone(row['session_id'])
        self.assertIsNone(row['config_dir'])
        self.assertIn('review-desk', self.register())

    def test_connector_post_as_preserves_existing_local_binding(self):
        routing.write_register(self.manifest, 'review-desk', 'post',
                               {'session_id': 'local-session', 'config_dir': str(self.root/'local-account')})
        with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.root/'service-account'), 'CLAUDE_CODE_SESSION_ID': 'service-session'}):
            self.assertTrue(self.connector_post()['ok'])
        row = self.roster()['review-desk']
        self.assertEqual(row['session_id'], 'local-session')
        self.assertEqual(row['config_dir'], str(self.root/'local-account'))

    def test_hook_retries_missing_pid_after_heartbeat(self):
        self._retry_hook_pid({})

    def test_cold_cli_hook_persists_manifest_identity(self):
        self.incumbent_row('anchor', 'sender', 'lead-a')
        result = subprocess.run([sys.executable, '-m', 'coordkit.cli', '--manifest', str(self.file), 'deliver', '--hook'],
                                input=json.dumps({'hook_event_name': 'PostToolUse', 'session_id': 's1'}),
                                text=True, capture_output=True, timeout=5,
                                env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src'),
                                         COORDKIT_AS='lead-a', CLAUDE_CODE_SESSION_ID='s1',
                                         CLAUDE_CONFIG_DIR=str(self.root/'account')))
        self.assertEqual((result.returncode, result.stderr), (0, ''))
        identity = self.register()['lead-a']['identity']
        self.assertEqual(identity['session_id'], 's1')
        self.assertEqual(identity['config_dir'], str(self.root/'account'))

    def test_hook_manifest_projects_identity_bindings_like_full_loader(self):
        from coordkit.adapters.claude_hook import load as hook_load
        self.file.write_text(self.file.read_text() + '\n[mcp]\nidentity="connector"\n'
                             '[questions]\nconfig_dirs=["account"]\n'
                             '[[lanes]]\nname="lead-a"\nconfig_dir="account"\ncwd="."\nmodel="opus"\neffort="high"\n')
        full, light = load(self.file), hook_load(self.file)
        self.assertEqual(light.mcp.identity, full.mcp.identity)
        self.assertEqual(light.escalation.internal_senders, full.escalation.internal_senders)
        self.assertEqual(light.questions.config_dirs, full.questions.config_dirs)
        self.assertEqual(light.addressed.internal_senders, full.addressed.internal_senders)
        self.assertEqual(light.addressed.heartbeat_every, full.addressed.heartbeat_every)
        self.assertEqual(routing.identity_sources(light, 'lead-a', env={}),
                         routing.identity_sources(full, 'lead-a', env={}))

    def test_hook_retries_dead_or_reused_pid_after_heartbeat(self):
        self._retry_hook_pid({'pid': 42, 'pid_started': 'old-process'})

    def _retry_hook_pid(self, pid):
        identity = dict(session_id='s1', transcript_path=str(self.root/'session.jsonl'), **pid)
        write_json(deliver.note_path(self.manifest, 'lead-a'), dict(name='lead-a', **identity))
        path = routing.home(self.manifest)/'register.json'
        write_json(path, {'lead-a': {'identity': dict(identity, refreshed_at=time.time())}})
        with patch.object(routing, 'identity_pid', return_value=None), patch.object(routing, 'probe_identity',
                side_effect=lambda record: dict(record, pid=99, pid_started='new-process')) as probe:
            routing.refresh_identity(self.manifest, 'lead-a')
            probe.assert_not_called()
            saved = self.register()
            saved['lead-a']['identity']['refreshed_at'] = time.time() - 10
            write_json(path, saved)
            routing.refresh_identity(self.manifest, 'lead-a')
            probe.assert_called_once()
        self.assertEqual(self.register()['lead-a']['identity']['pid'], 99)

    def test_hook_identity_without_a_transcript_does_not_import_orchestration(self):
        # Optional identity refresh must preserve the lightweight cold-hook boundary.
        code = '''import json, sys
from coordkit.cli import main
main(sys.argv[1:])
print(json.dumps(sorted(set(sys.modules) & {
    'coordkit.manifest', 'coordkit.usage', 'coordkit.adapters.claude_resume'})), file=sys.stderr)
'''
        self.incumbent_row('anchor', 'sender', 'lead-a')
        result = subprocess.run([sys.executable, '-c', code, '--manifest', str(self.file), 'deliver', '--hook'],
                                input=json.dumps(dict(hook_event_name='PostToolUse', session_id='s1')),
                                text=True, capture_output=True, timeout=10,
                                env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src'),
                                         COORDKIT_AS='lead-a', CLAUDE_CODE_SESSION_ID='s1'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stderr), [])
