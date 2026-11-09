"""Lane relocation contracts use synthetic accounts, fake holders and no live harness."""
import contextlib
from dataclasses import asdict
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import time
import tomllib
from unittest.mock import Mock, patch

from coordkit import addressed, bus, cli, lanemove, routing, usage
from coordkit.adapters import claude_resume
from coordkit.manifest import load
from coordkit.state import read_json, write_json
from tests.support import FakeCase


class LaneMoveTests(FakeCase):
    def setUp(self):
        super().setUp()
        self.file.write_text(self.file.read_text() + '''
[[lanes]]
name = "worker"
config_dir = "old-account"
cwd = "."
model = "opus"
effort = "high"
session_id = "session-one"
[[lanes]]
name = "other"
config_dir = "other-account"
cwd = "."
model = "sonnet"
effort = "low"
''')
        self.manifest = load(self.file)
        self.target = self.root/'new account'
        self.source = usage.transcript_dir(self.manifest.lanes[0])/'session-one.jsonl'
        self.source.parent.mkdir(parents=True)
        self.source.write_text(json.dumps(dict(type='assistant', timestamp=datetime.now(timezone.utc).isoformat(),
                                   message=dict(id='msg-1', model='claude-opus-4-5', usage=dict(input_tokens=100))))+'\n')
        os.utime(self.source, (time.time()-100, time.time()-100))
        self.original = self.file.read_bytes()
        for target, kwargs in (
            ('coordkit.lanemove.claude_resume.processes', dict(return_value={})),
            ('coordkit.lanemove.claude_resume.holder_present', dict(return_value=False)),
            ('coordkit.routing.probe_identity', dict(side_effect=lambda record: record)),
            ('os.kill', dict(side_effect=AssertionError('lane move must not signal'))),
            ('os.killpg', dict(side_effect=AssertionError('lane move must not signal'))),
        ):
            p = patch(target, **kwargs)
            mock = p.start(); self.addCleanup(p.stop)
            if target.endswith('holder_present'):
                self.holder = mock
        p = patch.dict(os.environ, HOME=str(self.root/'home'), CLAUDE_CONFIG_DIR='unrelated-inherited-account',
                       COORDKIT_AS='unrelated-inherited-lane')
        p.start(); self.addCleanup(p.stop)

    def invoke(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            result = lanemove.main(['worker', '--to-config', str(self.target), '--timeout', '0', *args], load(self.file))
        return result, out.getvalue()

    def plan(self):
        return read_json(next(lanemove.home(self.manifest).glob('*.json')))

    def rows(self):
        return bus.read_path(self.manifest.paths.bus_log)

    def frozen(self):
        with self.assertRaisesRegex(lanemove.Refusal, 'open_obligations'):
            self.invoke('--apply')
        plan = self.plan()
        with contextlib.redirect_stdout(io.StringIO()):
            bus.main(['ack', plan['id']+'-freeze-ask', 'done', '--from', 'worker'], self.manifest)
        return plan

    def hook(self, plan=None, *, old=False):
        plan = plan or self.plan()
        write_json(routing.home(self.manifest)/'hooks/worker.json', dict(
            name='worker', session_id='session-one', transcript_path=plan['destination'],
            at=time.time()-100 if old else time.time()))

    def complete(self):
        original = lanemove.resume
        def resumed(manifest, plan):
            original(manifest, plan)
            self.hook(plan)
        with patch.object(lanemove, 'resume', side_effect=resumed):
            return self.invoke('--apply', '--idle-seconds', '0')

    def test_plan_only_is_durable_and_never_edits_config_or_bus(self):
        self.invoke('--plan')
        plan = self.plan()
        self.assertEqual(plan['phase'], 'planned')
        self.assertEqual(self.file.read_bytes(), self.original)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.rows(), [])
        self.invoke()
        self.assertEqual(self.plan()['id'], plan['id'])
        self.assertEqual(plan['before_reading']['account'], 'old-account')
        self.assertEqual(plan['before_reading']['observed_model'], 'claude-opus-4-5')

    def test_freeze_requires_ack_and_existing_open_obligations(self):
        self.frozen()
        bus.configure(self.manifest)
        bus.append([dict(id='ancient', ts='2020-01-01T00:00:00Z', frm='lead', to='worker', kind='ask', text='old work')])
        with self.assertRaisesRegex(lanemove.Refusal, 'ancient'):
            self.invoke('--apply')
        self.assertEqual(self.file.read_bytes(), self.original)
        self.assertFalse(self.target.exists())
        self.assertEqual(len([r for r in self.rows() if r['id'].endswith('-freeze-ask')]), 1)

    def test_holder_timeout_posts_attention_and_retry_is_safe(self):
        self.frozen()
        self.holder.return_value = True
        for _ in range(2):
            with self.assertRaisesRegex(lanemove.Refusal, 'holder_present_timeout'):
                self.invoke('--apply')
        self.assertEqual(self.file.read_bytes(), self.original)
        self.assertFalse(self.target.exists())
        asks = [r for r in self.rows() if r['id'].endswith('-attention-holder_present_timeout')]
        self.assertEqual(len(asks), 1)
        self.holder.return_value = False
        self.assertEqual(self.complete()[0], 0)

    def test_idle_transcript_refuses_even_without_holder(self):
        self.frozen()
        self.source.touch()
        with self.assertRaisesRegex(lanemove.Refusal, 'transcript_active_timeout'):
            self.invoke('--apply')
        self.assertFalse(self.target.exists())

    def test_poll_waits_for_holder_absence_without_signals(self):
        self.frozen()
        self.holder.side_effect = [True, False, False, False, False, False, False]
        with patch('coordkit.lanemove.time.sleep') as sleep, patch('coordkit.lanemove.time.monotonic',
                side_effect=[0, 0, 0, 1]):
            lanemove.quiesce(self.manifest, self.plan(), 5, 0)
        sleep.assert_called_once()

    def test_operator_resume_copies_verifies_and_waits_for_fresh_hook(self):
        self.frozen()
        with patch('coordkit.lanemove.subprocess.Popen') as popen:
            with self.assertRaisesRegex(lanemove.Refusal, 'rearm_timeout'):
                self.invoke('--apply')
        popen.assert_not_called()
        plan = self.plan()
        self.assertEqual(Path(plan['destination']).read_bytes(), self.source.read_bytes())
        self.assertEqual(plan['copy_sha256'], lanemove.checksum(self.source))
        self.assertEqual(Path(plan['manifest_backup']).read_bytes(), self.original)
        self.assertEqual(load(self.file).lanes[0].config_dir, str(self.target))
        self.assertTrue(self.source.exists())
        self.assertEqual(shlex.split(plan['operator_command'].split(' && ')[1]),
                         ['env', 'CLAUDE_CONFIG_DIR='+str(self.target), 'COORDKIT_AS=worker',
                          'claude', '--resume', 'session-one'])
        self.hook(old=True)
        with self.assertRaisesRegex(lanemove.Refusal, 'rearm_timeout'):
            self.invoke('--apply')
        self.hook()
        self.assertEqual(self.invoke('--apply')[0], 0)
        self.assertEqual(self.plan()['completed'], list(lanemove.PHASES))
        self.assertEqual(self.plan()['after_reading']['account'], self.target.name)
        self.assertEqual(self.plan()['after_reading']['usage']['input_tokens'], 100)
        final = [r for r in self.rows() if r['kind'] == 'state' and r.get('subject') == 'lane-move']
        self.assertEqual(final[0]['state'], 'DONE')
        self.assertIn('session=session-one', final[0]['text'])
        self.assertTrue(any(r['id'] == plan['id'] and r['kind'] == 'handoff' for r in self.rows()))
        count = len(self.rows())
        self.invoke('--apply')
        self.assertEqual(len(self.rows()), count)

    def test_half_move_recovery_after_copy_before_manifest_receipt(self):
        self.frozen()
        original = lanemove.phase
        def fail(manifest, plan, name):
            if name == 'manifest':
                raise OSError('crash after manifest rename')
            return original(manifest, plan, name)
        with patch.object(lanemove, 'phase', side_effect=fail), self.assertRaises(OSError):
            self.invoke('--apply')
        plan = self.plan()
        self.assertNotIn('manifest', plan['completed'])
        self.assertEqual(load(self.file).lanes[0].config_dir, str(self.target))
        self.assertEqual(self.complete()[0], 0)
        self.assertEqual(self.plan()['id'], plan['id'])
        self.assertEqual(Path(self.plan()['manifest_backup']).read_bytes(), self.original)
        events = [json.loads(r) for r in self.manifest.paths.events.read_text().splitlines()]
        self.assertEqual(len(events), len(lanemove.PHASES))
        self.assertEqual(len({r['id'] for r in self.rows()}), len(self.rows()))

    def test_half_copy_recovery_and_checksum_mismatch_refusal(self):
        self.frozen()
        original = lanemove.copy_session
        def fail(plan):
            original(plan)
            raise OSError('crash after copying')
        with patch.object(lanemove, 'copy_session', side_effect=fail), self.assertRaises(OSError):
            self.invoke('--apply')
        destination = Path(self.plan()['destination'])
        destination.write_bytes(b'different session')
        with self.assertRaisesRegex(lanemove.Refusal, 'copy_checksum_mismatch'):
            self.invoke('--apply', '--idle-seconds', '0')
        self.assertEqual(destination.read_bytes(), b'different session')
        self.assertEqual(self.file.read_bytes(), self.original)
        destination.write_bytes(self.source.read_bytes())
        self.assertEqual(self.complete()[0], 0)

    def test_source_change_after_copy_refuses_recovery(self):
        self.frozen()
        with patch.object(lanemove, 'update_manifest', side_effect=OSError('crash')), self.assertRaises(OSError):
            self.invoke('--apply')
        self.source.write_text('{}\n')
        with self.assertRaisesRegex(lanemove.Refusal, 'source_changed'):
            self.invoke('--apply', '--idle-seconds', '0')
        self.assertEqual(self.file.read_bytes(), self.original)

    def test_headless_resume_exact_environment_and_no_duplicate_on_retry(self):
        self.file.write_text(self.file.read_text().replace('session_id = "session-one"', 'session_id = "session-one"\nresume="headless"'))
        self.frozen()
        with patch.object(lanemove.subprocess, 'Popen', return_value=Mock(pid=123)) as popen:
            with self.assertRaisesRegex(lanemove.Refusal, 'rearm_timeout'):
                self.invoke('--apply')
            with self.assertRaisesRegex(lanemove.Refusal, 'rearm_timeout'):
                self.invoke('--apply')
        popen.assert_called_once()
        args, kwargs = popen.call_args
        self.assertEqual(args[0][:3], ['claude', '--resume', 'session-one'])
        self.assertEqual(kwargs['env']['CLAUDE_CONFIG_DIR'], str(self.target))
        self.assertEqual(kwargs['env']['COORDKIT_AS'], 'worker')
        self.assertIn('--manifest '+shlex.quote(str(self.file)), args[0][4])
        self.assertEqual(kwargs['cwd'], str(self.root))
        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
        self.hook()
        self.invoke('--apply')
        self.assertEqual(self.plan()['launch']['pid'], 123)

    def test_unknown_launch_outcome_never_relaunches(self):
        self.file.write_text(self.file.read_text().replace('session_id = "session-one"', 'session_id = "session-one"\nresume="headless"'))
        self.frozen()
        with patch.object(lanemove.subprocess, 'Popen', side_effect=OSError('crash after intent')) as popen:
            with self.assertRaises(OSError):
                self.invoke('--apply')
            with self.assertRaisesRegex(lanemove.Refusal, 'rearm_timeout'):
                self.invoke('--apply')
        popen.assert_called_once()
        self.assertEqual(self.plan()['launch']['status'], 'intent')
        self.hook()
        self.invoke('--apply')

    def test_headless_refuses_holder_reappearing_before_launch(self):
        self.file.write_text(self.file.read_text().replace('session_id = "session-one"', 'session_id = "session-one"\nresume="headless"'))
        self.frozen()
        original = lanemove.resume
        def holder_returns(manifest, plan):
            self.holder.return_value = True
            original(manifest, plan)
        with patch.object(lanemove, 'resume', side_effect=holder_returns), patch.object(
                lanemove.subprocess, 'Popen') as popen, self.assertRaisesRegex(lanemove.Refusal, 'holder_present'):
            self.invoke('--apply')
        popen.assert_not_called()

    def test_manifest_edit_preserves_other_tables_and_unrelated_changes(self):
        self.frozen()
        self.file.write_text(self.file.read_text()+'\n# unrelated\n[wait]\nbackoff=2\n')
        before = tomllib.loads(self.file.read_text())
        self.complete()
        after = tomllib.loads(self.file.read_text())
        before['lanes'][0].update(config_dir=str(self.target), session_id='session-one')
        self.assertEqual(after, before)
        self.assertIn('# unrelated', self.file.read_text())

    def test_lane_manifest_change_and_different_target_refuse(self):
        self.invoke('--plan')
        with self.assertRaisesRegex(lanemove.Refusal, 'move_in_progress'):
            lanemove.main(['worker', '--to-config', str(self.root/'elsewhere')], self.manifest)
        self.file.write_text(self.file.read_text().replace('effort = "high"', 'effort="low"'))
        with self.assertRaisesRegex(lanemove.Refusal, 'manifest_lane_changed'):
            self.invoke('--apply')

    def test_unpinned_session_uses_roster_identity_and_is_pinned_on_apply(self):
        self.file.write_text(self.file.read_text().replace('session_id = "session-one"', ''))
        write_json(routing.home(self.manifest)/'hooks/worker.json', dict(name='worker', session_id='session-one',
                                                                      transcript_path=str(self.source), at=0))
        self.frozen()
        self.complete()
        self.assertEqual(load(self.file).lanes[0].session_id, 'session-one')

    def test_session_lock_blocks_move_and_cli_has_no_stop_command(self):
        self.frozen()
        with claude_resume.session_lock(self.manifest.lanes[0]), self.assertRaisesRegex(
                lanemove.Refusal, 'session_resume_locked'):
            self.invoke('--apply')
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.invoke('--stop-command', 'anything')
        self.assertFalse(self.target.exists())

    def test_stale_busy_transcript_and_old_wait_cursor_do_not_rearm(self):
        self.frozen()
        cursor = self.manifest.paths.bus_log.parent/'state/cursors/worker.wait'
        cursor.parent.mkdir(parents=True, exist_ok=True)
        cursor.write_text('0')
        with self.assertRaisesRegex(lanemove.Refusal, 'rearm_timeout'):
            self.invoke('--apply')
        cursor.touch()
        self.invoke('--apply')

    def test_cli_routes_plan_and_shadow_refuses(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--manifest', str(self.file), 'lane-move', 'worker',
                                       '--to-config', str(self.target), '--plan']), 0)
        self.file.write_text('mode="shadow"\n'+self.file.read_text())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['--manifest', str(self.file), 'lane-move', 'worker',
                                       '--to-config', str(self.target), '--apply']), 2)

    def test_fake_headless_binary_rearms_with_destination_identity(self):
        import sys
        self.file.write_text(self.file.read_text().replace('session_id = "session-one"', 'session_id = "session-one"\nresume="headless"'))
        self.frozen()
        bindir = self.root/'bin'
        bindir.mkdir()
        capture = self.root/'resume-capture.json'
        note = routing.home(self.manifest)/'hooks/worker.json'
        destination = self.plan()['destination']
        binary = bindir/'claude'
        binary.write_text('#!'+sys.executable+'\n' + f'''
import json, os, sys, time
from pathlib import Path
Path({str(capture)!r}).write_text(json.dumps(dict(argv=sys.argv[1:], cwd=os.getcwd(), config=os.environ['CLAUDE_CONFIG_DIR'])))
note = Path({str(note)!r})
note.parent.mkdir(parents=True, exist_ok=True)
note.write_text(json.dumps(dict(name='worker', session_id='session-one', transcript_path={destination!r}, at=time.time())))
''')
        binary.chmod(0o700)
        children = []
        actual = subprocess.Popen
        def spawn(*args, **kwargs):
            child = actual(*args, **kwargs)
            children.append(child)
            return child
        try:
            with patch.dict(os.environ, PATH=str(bindir)+os.pathsep+os.environ['PATH']), patch.object(
                    lanemove.subprocess, 'Popen', side_effect=spawn):
                self.assertEqual(self.invoke('--apply', '--timeout', '5')[0], 0)
        finally:
            for child in children:
                child.wait(timeout=5)  # bounded fake exits on its own; never signal it
        observed = read_json(capture)
        self.assertEqual(observed['argv'][:3], ['--resume', 'session-one', '-p'])
        self.assertEqual(observed['config'], str(self.target))
        self.assertEqual(observed['cwd'], str(self.root))
        self.assertEqual(len(children), 1)

    def test_holder_probe_failure_fails_closed(self):
        self.frozen()
        self.holder.side_effect = OSError('lsof unavailable')
        with self.assertRaisesRegex(OSError, 'lsof unavailable'):
            self.invoke('--apply')
        self.assertFalse(self.target.exists())
        self.assertEqual(self.file.read_bytes(), self.original)

    def test_destination_conflict_is_never_overwritten(self):
        self.frozen()
        destination = Path(self.plan()['destination'])
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b'unrelated')
        with self.assertRaisesRegex(lanemove.Refusal, 'copy_checksum_mismatch'):
            self.invoke('--apply', '--idle-seconds', '0')
        self.assertEqual(destination.read_bytes(), b'unrelated')
        self.assertEqual(self.file.read_bytes(), self.original)

    def test_symlink_session_refused(self):
        self.frozen()
        destination = Path(self.plan()['destination'])
        destination.parent.mkdir(parents=True)
        destination.symlink_to(self.source)
        with self.assertRaisesRegex(lanemove.Refusal, 'destination_symlink'):
            self.invoke('--apply', '--idle-seconds', '0')
        self.assertEqual(self.file.read_bytes(), self.original)

    def test_move_lock_refuses_concurrent_apply(self):
        self.invoke('--plan')
        with lanemove.locked(lanemove.home(self.manifest)/'worker.lock'), self.assertRaisesRegex(
                lanemove.Refusal, 'move_locked'):
            self.invoke('--apply')
        self.assertFalse(self.rows())

    def test_crash_after_bus_publish_replays_one_phase_event(self):
        self.frozen()
        real_save = lanemove.save
        def crash(plan):
            if plan['phase'] == 'freeze':
                raise OSError('lost freeze receipt')
            return real_save(plan)
        with patch.object(lanemove, 'save', side_effect=crash), self.assertRaises(OSError):
            self.invoke('--apply')
        self.complete()
        events = [json.loads(row) for row in self.manifest.paths.events.read_text().splitlines()]
        self.assertEqual(len(events), len(lanemove.PHASES))
        self.assertEqual(len({r['id'] for r in self.rows()}), len(self.rows()))

    def test_completed_move_allows_next_move_to_another_account(self):
        self.frozen()
        self.complete()
        self.target = self.root/'third-account'
        self.invoke('--plan')
        plans = [read_json(path) for path in lanemove.home(self.manifest).glob('*.json')]
        self.assertEqual(len(plans), 2)
        pending = next(plan for plan in plans if 'done' not in plan['completed'])
        self.assertEqual(pending['before']['config_dir'], str(self.root/'new account'))

    def test_headless_revalidates_copy_after_manifest_pause(self):
        self.file.write_text(self.file.read_text().replace('session_id = "session-one"', 'session_id = "session-one"\nresume="headless"'))
        self.frozen()
        with patch.object(lanemove, 'resume', side_effect=OSError('pause before launch')), self.assertRaises(OSError):
            self.invoke('--apply')
        destination = Path(self.plan()['destination'])
        destination.write_bytes(b'changed copy')
        with patch.object(lanemove.subprocess, 'Popen') as popen, self.assertRaisesRegex(
                lanemove.Refusal, 'copy_checksum_mismatch'):
            self.invoke('--apply')
        popen.assert_not_called()
        destination.write_bytes(self.source.read_bytes())
        self.source.write_bytes(b'old lane ran again')
        with patch.object(lanemove.subprocess, 'Popen') as popen, self.assertRaisesRegex(
                lanemove.Refusal, 'source_changed'):
            self.invoke('--apply')
        popen.assert_not_called()

    def test_shared_state_cannot_reuse_another_manifest_move(self):
        self.invoke('--plan')
        other = self.root/'other.toml'
        other.write_bytes(self.file.read_bytes())
        with self.assertRaisesRegex(lanemove.Refusal, 'move_manifest_mismatch'):
            lanemove.main(['worker', '--to-config', str(self.target), '--apply'], load(other))
        self.assertFalse(self.rows())
