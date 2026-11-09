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
import sys
import time
import tomllib
from unittest.mock import Mock, patch

from coordkit import addressed, bus, cli, lanemove, routing, usage
from coordkit.adapters import claude_resume
from coordkit.manifest import load
from coordkit.state import read_json, write_json
from tests.support import FakeCase

REAL_PROCESSES = claude_resume.processes


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
            ('coordkit.actl.AgentctlClient.lane_subscriptions', dict(return_value=[])),
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
        self.rearm_patch = patch.object(lanemove, 'rearm_all', return_value=True)
        self.rearm_patch.start(); self.addCleanup(self.rearm_patch.stop)

    def invoke(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            result = lanemove.main(['worker', '--to-config', str(self.target), '--timeout', '0', *args], load(self.file))
        return result, out.getvalue()

    def plan(self):
        return next(read_json(path) for path in lanemove.home(self.manifest).glob('*.json')
                    if 'plan_file' in read_json(path))

    def rows(self):
        return bus.read_path(self.manifest.paths.bus_log)

    def frozen(self):
        with self.assertRaisesRegex(lanemove.Refusal, 'open_obligations'):
            self.invoke('--apply')
        plan = self.plan()
        with contextlib.redirect_stdout(io.StringIO()):
            bus.main(['ack', plan['id']+'-freeze-ask', 'done', '--from', 'worker'], self.manifest)
        state = read_json(Path(plan['move_state_file']))
        state['inventory_complete'] = True
        write_json(Path(plan['move_state_file']), state)
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

    def test_final_state_records_complete_restart_inventory_and_exit_answer(self):
        self.frozen()
        path = Path(self.plan()['move_state_file'])
        state = read_json(path)
        self.assertTrue({'goal', 'open_prs', 'lanes_in_flight', 'bus_name', 'next_steps',
                         'background_shells', 're_arms'} <= state.keys())
        self.assertEqual(state['re_arms'][0]['argv'][-3:], ['--to', 'worker', '--own-asks'])
        state.update(goal='Finish the release', open_prs=[17], lanes_in_flight=[],
                     next_steps=['Check the canary'])
        for kind in ('bus-waiter', 'monitor', 'watcher', 'background-shell'):
            argv = ['bus', 'wait', '--persistent', '--to', 'other', '--kinds', 'answer,state'] if kind == 'bus-waiter' else ['check-'+kind]
            state['re_arms'].append(dict(name='extra-'+kind, kind=kind, argv=argv))
        state['background_shells'] = [dict(pid=22, rearm='extra-background-shell')]
        write_json(path, state)
        result, printed = self.complete()
        self.assertEqual(result, 0)
        recorded = self.plan()['move_state']
        self.assertEqual(recorded['re_arms'], state['re_arms'])
        self.assertEqual(recorded['goal'], state['goal'])
        self.assertEqual(recorded['waiters'][-1], dict(name='extra-bus-waiter',
                                                     flags=['--persistent', '--to', 'other', '--kinds', 'answer,state']))
        self.assertIn('"Exit and stop tasks" (stop background shells)', printed)
        self.assertEqual(read_json(path)['background_shells'], state['background_shells'])

    def test_inventory_must_be_confirmed_and_cover_observed_children(self):
        self.frozen()
        plan = self.plan()
        path = Path(plan['move_state_file'])
        state = read_json(path)
        state['inventory_complete'] = False
        write_json(path, state)
        with self.assertRaisesRegex(lanemove.Refusal, 'move_state_inventory_required'):
            self.invoke('--apply')
        state['inventory_complete'] = True
        write_json(path, state)
        plan['holders'] = {'11': dict(started='parent', argv='claude'),
                           '22': dict(started='shell', argv='sh watch', ppid=11)}
        lanemove.save(plan)
        with self.assertRaisesRegex(lanemove.Refusal, 'background_shell_inventory_required'):
            self.invoke('--apply')
        self.assertFalse(self.target.exists())

    def test_reparented_background_shell_blocks_quiesce_without_session_arg(self):
        self.frozen()
        plan = self.plan()
        parent = dict(started='parent', argv='claude --resume session-one', ppid=1)
        shell = dict(started='shell', argv='/bin/sh monitor', ppid=11)
        with patch.object(claude_resume, 'processes', return_value={11: parent, 22: shell}):
            with self.assertRaisesRegex(lanemove.Refusal, 'background_holder_present_timeout'):
                lanemove.quiesce(self.manifest, plan, 0, 0)
        shell = dict(shell, ppid=1)
        with patch.object(claude_resume, 'processes', return_value={22: shell}):
            with self.assertRaisesRegex(lanemove.Refusal, 'background_holder_present_timeout'):
                lanemove.quiesce(self.manifest, plan, 0, 0)
        with patch.object(claude_resume, 'processes', return_value={22: dict(shell, started='reused')}), patch.object(
                claude_resume, 'transcript_holders', return_value=set()):
            lanemove.quiesce(self.manifest, plan, 0, 0)

    def test_open_transcript_parent_without_resume_arg_tracks_its_shells(self):
        self.frozen()
        plan = self.plan()
        with patch.object(claude_resume, 'processes', return_value={
                11: dict(started='parent', argv='claude', ppid=1),
                22: dict(started='shell', argv='/bin/sh monitor', ppid=11)}), patch.object(
                claude_resume, 'transcript_holders', return_value={11}):
            with self.assertRaisesRegex(lanemove.Refusal, 'background_holder_present_timeout'):
                lanemove.quiesce(self.manifest, plan, 0, 0)
        self.assertIn('22', self.plan()['holders'])

    def test_explicitly_recorded_detached_shell_is_also_a_holder(self):
        self.frozen()
        plan = self.plan()
        state = read_json(Path(plan['move_state_file']))
        state['background_shells'] = [dict(pid=22, started='shell', rearm='heartbeat')]
        write_json(Path(plan['move_state_file']), state)
        with patch.object(claude_resume, 'processes', return_value={
                22: dict(started='shell', argv='/bin/sh watch', ppid=1)}), patch.object(
                claude_resume, 'transcript_holders', return_value=set()):
            lanemove.move_state(self.manifest, plan)
            with self.assertRaisesRegex(lanemove.Refusal, 'background_holder_present_timeout'):
                lanemove.quiesce(self.manifest, plan, 0, 0)

    def rearm_fixture(self):
        self.rearm_patch.stop()
        self.frozen()
        plan = self.plan()
        lanemove.final_state(self.manifest, plan)
        return plan

    def test_rearm_starts_and_verifies_every_recorded_entry_with_receipts(self):
        plan = self.rearm_fixture()
        entries = plan['move_state']['re_arms']
        entries += [dict(name=kind, kind=kind, argv=['fake-'+kind])
                    for kind in ('monitor', 'watcher', 'background-shell')]
        entries.append(dict(name='other-waiter', kind='bus-waiter',
                            argv=['bus', 'wait', '--persistent', '--to', 'other', '--kinds', 'answer,state']))
        processes, commands = {}, []
        def spawn(argv, **kwargs):
            self.assertEqual(self.plan()['rearm_outcomes'][entries[len(commands)]['name']]['status'], 'intent')
            pid = 200 + len(commands)
            commands.append(argv)
            processes[pid] = dict(started='started', argv=shlex.join(argv))
            if 'wait' in argv:
                cursor = self.manifest.paths.bus_log.parent/'state/cursors'/(argv[argv.index('--to')+1]+'.wait')
                cursor.parent.mkdir(parents=True, exist_ok=True)
                cursor.write_text('0')
                write_json(cursor.with_suffix('.wait.owner.json'), dict(pid=pid, started='started', session_id='session-one'))
            return Mock(pid=pid)
        status = subprocess.CompletedProcess([], 0, json.dumps(dict(event='arm', owner='201:timer')), '')
        with patch.object(lanemove.subprocess, 'Popen', side_effect=spawn), patch.object(
                claude_resume, 'start_time', return_value='started'), patch.object(
                claude_resume, 'processes', return_value=processes), patch.object(
                lanemove, 'verifier_output', return_value=status.stdout):
            self.assertTrue(lanemove.rearm_all(self.manifest, plan))
            self.assertTrue(lanemove.rearm_all(self.manifest, plan))
            processes.pop(203)  # A lane-wide hook cannot hide a missing watcher.
            self.hook()
            self.assertFalse(lanemove.rearm_all(self.manifest, plan))
        self.assertEqual(commands, [entry['argv'] for entry in entries])
        outcomes = self.plan()['rearm_outcomes']
        self.assertEqual(set(outcomes), {entry['name'] for entry in entries})
        self.assertFalse(outcomes['watcher']['verified'])
        self.assertTrue(outcomes['worker']['verified'])

    def test_no_rearm_records_each_skip_and_still_requires_verification(self):
        plan = self.rearm_fixture()
        with patch.object(lanemove.subprocess, 'Popen') as spawn:
            self.assertFalse(lanemove.rearm_all(self.manifest, plan, no_rearm=True))
        spawn.assert_not_called()
        outcomes = self.plan()['rearm_outcomes']
        self.assertEqual(set(outcomes), {'worker', 'heartbeat'})
        self.assertTrue(all(o['no_rearm'] and o['status'] == 'skipped' and not o['verified'] for o in outcomes.values()))

    def test_no_rearm_verifies_manual_entries_and_cli_passes_exception(self):
        plan = self.rearm_fixture()
        state = read_json(Path(plan['move_state_file']))
        snapshot = {}
        for index, entry in enumerate(state['re_arms']):
            entry.update(pid=200+index, started='manual')
            snapshot[200+index] = dict(started='manual', argv=shlex.join(entry['argv']))
        write_json(Path(plan['move_state_file']), state)
        cursor = self.manifest.paths.bus_log.parent/'state/cursors/worker.wait'
        cursor.parent.mkdir(parents=True, exist_ok=True)
        status = subprocess.CompletedProcess([], 0, json.dumps(dict(event='arm', owner='201:timer')), '')
        with patch.object(lanemove.subprocess, 'Popen') as spawn, patch.object(
                claude_resume, 'processes', return_value=snapshot), patch.object(
                lanemove, 'verifier_output', return_value=status.stdout):
            self.assertFalse(lanemove.rearm_all(self.manifest, plan, no_rearm=True))
            cursor.write_text('0')
            write_json(cursor.with_suffix('.wait.owner.json'), dict(pid=200, started='manual', session_id='session-one'))
            self.assertTrue(lanemove.rearm_all(self.manifest, plan, no_rearm=True))
        spawn.assert_not_called()
        with patch.object(lanemove, 'rearmed', return_value=True), patch.object(
                lanemove, 'rearm_all', return_value=True) as rearm:
            self.invoke('--apply', '--no-rearm', '--idle-seconds', '0')
        self.assertTrue(rearm.call_args.args[-1])

    def test_no_rearm_can_adopt_manually_inspected_uncertain_spawn(self):
        plan = self.rearm_fixture()
        state = read_json(Path(plan['move_state_file']))
        for index, entry in enumerate(state['re_arms']):
            entry.update(pid=200+index, started='manual')
        write_json(Path(plan['move_state_file']), state)
        plan['rearm_outcomes'] = {e['name']: dict(status='intent', requested_at=time.time()-10)
                                  for e in state['re_arms']}
        cursor = self.manifest.paths.bus_log.parent/'state/cursors/worker.wait'
        cursor.parent.mkdir(parents=True, exist_ok=True)
        cursor.write_text('0')
        write_json(cursor.with_suffix('.wait.owner.json'), dict(pid=200, started='manual', session_id='session-one'))
        snapshot = {e['pid']: dict(started=e['started'], argv=shlex.join(e['argv'])) for e in state['re_arms']}
        status = subprocess.CompletedProcess([], 0, json.dumps(dict(event='arm', owner='201:manual')), '')
        with patch.object(lanemove.subprocess, 'Popen') as spawn, patch.object(
                claude_resume, 'processes', return_value=snapshot), patch.object(
                lanemove, 'verifier_output', return_value=status.stdout):
            self.assertTrue(lanemove.rearm_all(self.manifest, plan, no_rearm=True))
        spawn.assert_not_called()
        self.assertTrue(all(o['manual_recovery_at'] for o in self.plan()['rearm_outcomes'].values()))

    def test_rearm_crash_intent_never_duplicates_launch(self):
        plan = self.rearm_fixture()
        with patch.object(lanemove.subprocess, 'Popen', side_effect=RuntimeError('crash after intent')) as spawn:
            with self.assertRaisesRegex(RuntimeError, 'crash after intent'):
                lanemove.rearm_all(self.manifest, plan)
        self.assertEqual(self.plan()['rearm_outcomes']['worker']['status'], 'intent')
        with patch.object(lanemove.subprocess, 'Popen', side_effect=OSError('missing tool')) as spawn:
            self.assertFalse(lanemove.rearm_all(self.manifest, self.plan()))
        self.assertEqual(spawn.call_count, 1)  # Only the heartbeat, never the uncertain waiter.
        self.assertEqual(self.plan()['rearm_outcomes']['heartbeat']['status'], 'failed')

    def test_rearm_environment_binds_lane_and_drops_movers_process_identity(self):
        self.invoke('--plan')
        with patch.dict(os.environ, CLAUDE_PID='123', CLAUDE_CODE_SESSION_ID='mover-session',
                        AGENTCTL_EXECUTION_ID='mover-exec', CODEX_THREAD_ID='mover-thread'):
            env = lanemove.rearm_env(self.plan(), dict(env={'AGENT_HEARTBEAT_KEY': 'session-one'}))
        self.assertEqual(env['CLAUDE_CODE_SESSION_ID'], 'session-one')
        self.assertEqual(env['COORDKIT_AS'], 'worker')
        self.assertEqual(env['CLAUDE_CONFIG_DIR'], str(self.target))
        self.assertEqual(env['AGENT_HEARTBEAT_KEY'], 'session-one')
        self.assertTrue({'CLAUDE_PID', 'AGENTCTL_EXECUTION_ID', 'CODEX_THREAD_ID'}.isdisjoint(env))

    def test_real_rearm_children_arm_wait_cursor_heartbeat_and_monitor(self):
        plan = self.rearm_fixture()
        entries = plan['move_state']['re_arms']
        entries[0]['argv'] += ['--poll', '0.03']
        finish, status = self.root/'finish', self.root/'heartbeat.json'
        # A cooperative fixture ends the otherwise unbounded consumer without signals.
        wrapper = f"""import sys
from pathlib import Path
from coordkit import bus, cli
sleep = bus.time.sleep
def pause(seconds):
    if Path({str(self.root/'finish')!r}).exists():
        raise SystemExit(0)
    sleep(seconds)
bus.time.sleep = pause
raise SystemExit(cli.main(sys.argv[1:]))
"""
        entries[0]['argv'] = [sys.executable, '-c', wrapper, *entries[0]['argv'][3:]]

        binary = self.root/'agent-heartbeat'
        binary.write_text('#!'+sys.executable+'\n'+f'''
import json, os, sys, time
from pathlib import Path
status, finish = Path({str(status)!r}), Path({str(finish)!r})
if sys.argv[1] == 'status':
    print(status.read_text() if status.exists() else '{{}}')
else:
    if sys.argv[1] == '50':
        status.write_text(json.dumps(dict(event='arm', owner=str(os.getpid())+':fake')))
    until = time.monotonic()+90
    while not finish.exists() and time.monotonic() < until:
        time.sleep(.02)
''')
        binary.chmod(0o700)
        entries[1]['argv'][0] = str(binary)
        entries[1]['verify_argv'][0] = str(binary)
        entries.append(dict(name='monitor', kind='monitor', argv=[str(binary), 'monitor']))
        children = []
        actual = subprocess.Popen
        def spawn(*args, **kwargs):
            child = actual(*args, **kwargs)
            if kwargs.get('start_new_session'):
                children.append(child)
            return child
        # Persistent consumers must retain a receiver after the first reply.
        bus.configure(self.manifest)
        bus.append([dict(id='mine', ts='2026-01-01T00:00:00Z', frm='worker', to='other', kind='ask', text='help')])
        try:
            with patch.object(lanemove.subprocess, 'Popen', side_effect=spawn), patch.object(
                    claude_resume, 'processes', side_effect=REAL_PROCESSES), patch.dict(
                    os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src')), patch.object(
                    claude_resume, 'PROBE_TIMEOUT', 30):
                # Real process probes and verifier startup share the loaded host.
                # Keep a bounded deadline without racing the fixture's own exit.
                deadline = time.monotonic()+60
                while not lanemove.rearm_all(self.manifest, plan) and time.monotonic() < deadline:
                    time.sleep(.03)
                self.assertTrue(all(o['verified'] for o in plan['rearm_outcomes'].values()), plan['rearm_outcomes'])
                bus.configure(self.manifest)
                bus.append([dict(id='reply', ts='2026-01-01T00:00:01Z', frm='other', to='worker',
                                 kind='fyi', re='mine', text='reply under another kind')])
                deadline = time.monotonic()+15
                log = Path(plan['rearm_outcomes']['worker']['log'])
                while 'reply under another kind' not in log.read_text() and time.monotonic() < deadline:
                    time.sleep(.03)
                self.assertIsNone(children[0].poll())
                bus.append([dict(id='second', ts='2026-01-01T00:00:02Z', frm='other', to='worker',
                                 kind='ask', text='second work item')])
                deadline = time.monotonic()+15
                while 'second work item' not in log.read_text() and time.monotonic() < deadline:
                    time.sleep(.03)
                self.assertIn('second work item', log.read_text())
                finish.touch()
                self.assertEqual(children[0].wait(timeout=30), 0)
        finally:
            finish.touch()
            for child in children:
                child.wait(timeout=100)  # All fakes and the cooperative waiter exit naturally.
        self.assertEqual(len(children), 3)
        self.assertIn('reply under another kind', Path(plan['rearm_outcomes']['worker']['log']).read_text())

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
        self.assertNotIn(str(self.source), final[0]['text'])
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
        with self.assertRaisesRegex(lanemove.Refusal, 'rearm_timeout'):
            self.invoke('--apply')
        self.hook()
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
        plans = [read_json(path) for path in lanemove.home(self.manifest).glob('*.json')
                 if 'plan_file' in read_json(path)]
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

    def test_resume_rejects_old_waiter_even_when_its_cursor_is_fresh(self):
        self.frozen()
        plan = self.plan()
        plan['resume_requested_at'] = time.time()-1
        cursor = self.manifest.paths.bus_log.parent/'state/cursors/worker.wait'
        cursor.parent.mkdir(parents=True, exist_ok=True)
        cursor.touch()
        with patch.object(claude_resume, 'processes', return_value={
                77: dict(started='old', argv='coordkit bus wait --to worker')}):
            self.assertFalse(lanemove.rearmed(self.manifest, plan))
        self.hook(plan)
        self.assertTrue(lanemove.rearmed(self.manifest, plan))

    def test_existing_waiter_refuses_spawn_and_pid_reuse_cannot_verify(self):
        plan = self.rearm_fixture()
        entry = plan['move_state']['re_arms'][0]
        current = {77: dict(started='old', argv=shlex.join(entry['argv']))}
        with patch.object(claude_resume, 'processes', return_value=current), patch.object(
                lanemove.subprocess, 'Popen') as spawn, self.assertRaisesRegex(lanemove.Refusal, 'duplicate_waiter'):
            lanemove.rearm_all(self.manifest, plan)
        spawn.assert_not_called()
        self.assertFalse(lanemove.verify_rearm(self.manifest, plan, entry,
                                             dict(pid=77, started='reused', requested_at=0), current))

    def test_held_cursor_refuses_waiter_even_without_process_snapshot(self):
        plan = self.rearm_fixture()
        entry = plan['move_state']['re_arms'][0]
        lock = self.manifest.paths.bus_log.parent/'state/cursors/worker.wait.lock'
        with lanemove.locked(lock), patch.object(lanemove.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(lanemove.Refusal, 'move_locked'):
                lanemove.rearm_all(self.manifest, plan)
        spawn.assert_not_called()

    def test_bus_refs_only_expose_summary_and_private_receipts_stay_0600(self):
        from coordkit.refs import ref_file
        self.manifest.mcp.ref_roots = [str(self.manifest.paths.root)]
        plan = lanemove.prepare(self.manifest, 'worker', str(self.target))
        self.assertFalse(lanemove.exposed(self.manifest, Path(plan['plan_file'])))
        plan['holders'] = {'77': dict(argv='example-command private-value')}
        plan['move_state'] = dict(re_arms=[dict(env={'EXAMPLE_VALUE': 'private-value'})])
        Path(plan['plan_file']).chmod(0o644)
        lanemove.save(plan)
        with contextlib.redirect_stdout(io.StringIO()):
            lanemove.publish(self.manifest, plan, 'check', 'handoff', 'Redacted move summary')
        row = self.rows()[-1]
        public = ref_file(self.manifest, row).read_text()
        self.assertNotIn('private-value', public)
        self.assertNotIn(plan['plan_file'], json.dumps(row))
        self.assertNotIn('argv', public)
        self.assertNotIn('env', public)
        self.assertEqual(Path(plan['plan_file']).stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(ValueError, 'outside'):
            ref_file(self.manifest, dict(ref=plan['plan_file']))

    def test_receipt_scan_and_receipt_bytes_fail_closed_at_caps(self):
        self.invoke('--plan')
        with patch.object(lanemove, 'MAX_RECEIPTS', 0), self.assertRaisesRegex(
                lanemove.Refusal, 'receipt_scan_limit'):
            self.invoke('--plan')
        with patch.object(lanemove, 'MAX_RECEIPT_BYTES', 1), self.assertRaisesRegex(
                lanemove.Refusal, 'receipt_size_limit'):
            self.invoke('--plan')

    def test_inflight_inventory_requires_an_execution_id_before_move_can_finish(self):
        # An omitted or malformed ID must never make a lane's callback optional.
        plan = self.frozen()
        path = Path(plan['move_state_file'])
        state = read_json(path)
        for entry in (dict(name='child'), dict(name='child', exec_id=''),
                      dict(name='child', execution_id=' '), dict(name='child', exec_id=42),
                      dict(name='child', execution_id=[]), 'child', None):
            with self.subTest(entry=entry):
                state['lanes_in_flight'] = [entry]
                write_json(path, state)
                with self.assertRaisesRegex(lanemove.Refusal, 'inflight_execution_id_required') as error:
                    self.complete()
                self.assertIn('child' if isinstance(entry, dict) else 'lanes_in_flight[0]', error.exception.detail)
                self.assertNotIn('done', self.plan()['completed'])
                self.assertEqual(self.file.read_bytes(), self.original)

    def test_inflight_inventory_must_be_a_list(self):
        plan = self.frozen()
        path = Path(plan['move_state_file'])
        state = read_json(path)
        for value in (None, {}, 'child'):
            with self.subTest(value=value):
                state['lanes_in_flight'] = value
                write_json(path, state)
                with self.assertRaisesRegex(lanemove.Refusal, 'inflight_inventory_required'):
                    lanemove.final_state(self.manifest, plan)

    def test_phase_skips_malformed_event_rows(self):
        self.invoke('--plan')
        plan = self.plan()
        events = self.manifest.paths.events
        events.parent.mkdir(parents=True, exist_ok=True)
        events.write_text('{"id":"other"}\n{not json\n[]\n')
        with contextlib.redirect_stdout(io.StringIO()):
            lanemove.phase(self.manifest, plan, 'freeze')
            lanemove.phase(self.manifest, self.plan(), 'freeze')
        seen = []
        for line in events.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                seen.append(row.get('id'))
        self.assertEqual(seen.count(plan['id'] + '-freeze'), 1)

    def test_resume_verification_does_not_consume_the_rearm_budget(self):
        self.frozen()
        with self.assertRaisesRegex(lanemove.Refusal, 'rearm_timeout'):
            self.invoke('--apply')
        clock = {'now': 0.0}

        def monotonic():
            return clock['now']

        def sleep(seconds):
            clock['now'] += seconds

        def resumed(manifest, plan):
            return clock['now'] >= 50

        def arms(manifest, plan, no_rearm=False, entries=None):
            return clock['now'] >= 70

        with patch('coordkit.lanemove.time.monotonic', monotonic), patch(
                'coordkit.lanemove.time.sleep', sleep), patch.object(
                lanemove, 'rearmed', side_effect=resumed), patch.object(
                lanemove, 'rearm_all', side_effect=arms):
            self.assertEqual(lanemove.apply(load(self.file), self.plan(), 60, 0), 0)
        self.assertGreaterEqual(clock['now'], 70)
        self.assertLess(clock['now'], 110)
        self.assertIn('done', self.plan()['completed'])

    def test_publication_refs_preserve_phase_attention_and_handoff_evidence(self):
        # Later phases and replays must not change the evidence of an older row.
        from coordkit.refs import ref_file
        plan = self.frozen()
        with contextlib.redirect_stdout(io.StringIO()):
            lanemove.phase(self.manifest, plan, 'freeze')
            with self.assertRaisesRegex(lanemove.Refusal, 'example_pause'):
                lanemove.attention(self.manifest, plan, 'example_pause')
        earlier = [(row, ref_file(self.manifest, row).read_bytes()) for row in self.rows() if row.get('ref')]
        self.complete()
        with contextlib.redirect_stdout(io.StringIO()):
            lanemove.phase(self.manifest, self.plan(), 'freeze')
        for row, original in earlier:
            self.assertEqual(ref_file(self.manifest, row).read_bytes(), original)
        rows = {row['id']: row for row in self.rows() if row.get('ref') and not row.get('copy_of')}
        self.assertEqual(len({row['ref'] for row in rows.values()}), len(rows))
        freeze = read_json(ref_file(self.manifest, rows[plan['id']+'-freeze']))
        self.assertEqual(freeze['phase'], 'freeze')
        self.assertNotIn('done', freeze['completed'])
        done = read_json(ref_file(self.manifest, rows[plan['id']+'-done']))
        self.assertEqual(done['phase'], 'done')
        self.assertNotEqual(rows[plan['id']]['ref'], rows[plan['id']+'-done']['ref'])

    def test_publication_reuses_atomic_summary_after_bus_failure(self):
        plan = self.frozen()
        with patch.object(lanemove.bus, 'main', return_value=1), self.assertRaisesRegex(
                lanemove.Refusal, 'publication_failed'):
            lanemove.publish(self.manifest, plan, 'freeze', 'fyi', 'Frozen')
        directory = self.manifest.path(self.manifest.mcp.ref_roots[0])/'lane-move-summaries'
        summary = directory/(plan['id']+'-freeze.json')
        original = summary.read_bytes()
        plan['completed'].append('done')
        with patch.object(lanemove.os, 'link', wraps=os.link) as link:
            with contextlib.redirect_stdout(io.StringIO()):
                lanemove.publish(self.manifest, plan, 'freeze', 'fyi', 'Frozen')
        self.assertEqual(link.call_args.args[1], summary)
        self.assertEqual(summary.read_bytes(), original)
        self.assertEqual(self.rows()[-1]['ref'], str(summary))
        self.assertFalse(list(directory.glob('.lane-move-*')))

    def test_subscription_inventory_covers_inflight_and_recreates_on_new_host(self):
        plan = self.rearm_fixture()
        path = Path(plan['move_state_file'])
        state = read_json(path)
        state['lanes_in_flight'] = [dict(exec_id='exec-example')]
        write_json(path, state)
        with self.assertRaisesRegex(lanemove.Refusal, 'subscription_inventory_required'):
            lanemove.final_state(self.manifest, plan)
        old = dict(execution_id='exec-example', subscription_id='sub-old', source_host='old-host',
                   target='/old/events.ndjson', expires_at='2020-01-01T00:00:00Z', kinds=['terminal', 'attention'])
        state['subscriptions'] = [old]
        for key in ('exec_id', 'execution_id'):
            with self.subTest(key=key):
                state['lanes_in_flight'] = [{key: 'exec-example'}]
                write_json(path, state)
                lanemove.final_state(self.manifest, plan)
        fresh = dict(old, source_host='new-host', target=str(self.manifest.paths.events),
                     subscription_id='sub-new', expires_at='2999-01-01T00:00:00Z', state='active')
        with patch.object(lanemove.socket, 'gethostname', return_value='new-host'), patch.object(
                lanemove.AgentctlClient, 'subscribe', return_value=dict(id='sub-new')) as create, patch.object(
                lanemove.AgentctlClient, 'lane_subscriptions', return_value=[fresh]):
            self.assertTrue(lanemove.rearm_subscriptions(self.manifest, plan, False))
            self.assertTrue(lanemove.rearm_subscriptions(self.manifest, plan, False))
            create.assert_called_once_with('exec-example', kinds=['terminal', 'attention'])
        with patch.object(lanemove.socket, 'gethostname', return_value='new-host'), patch.object(
                lanemove.AgentctlClient, 'lane_subscriptions', return_value=[old]):
            self.assertFalse(lanemove.rearm_subscriptions(self.manifest, plan, False))

    def test_slow_verifier_finishes_without_signals_or_duplicate_launch(self):
        self.invoke('--plan')
        plan = self.plan()
        outcome = dict(pid=991)
        entry = dict(verify_argv=[sys.executable, '-c', 'import time; time.sleep(2.2); print("ready")'])
        actual, children = subprocess.Popen, []
        def spawn(*args, **kwargs):
            child = actual(*args, **kwargs)
            children.append(child)
            return child
        with patch.object(lanemove.subprocess, 'Popen', side_effect=spawn), patch.dict(
                os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src')):
            try:
                self.assertIsNone(lanemove.verifier_output(plan, entry, outcome))
                self.assertIsNone(lanemove.verifier_output(plan, entry, outcome))
                self.assertEqual(len(children), 1)
                children[0].wait(timeout=15)
                self.assertEqual(lanemove.verifier_output(plan, entry, outcome), 'ready')
            finally:
                for child in children:
                    child.wait(timeout=15)

    def test_paused_legacy_receipt_discards_cursor_based_resume_verification(self):
        self.invoke('--plan')
        plan = self.plan()
        plan.update(version=1, resume_verified_at=time.time())
        lanemove.save(plan)
        self.invoke('--plan')
        self.assertEqual(self.plan()['version'], 2)
        self.assertNotIn('resume_verified_at', self.plan())

    def test_legacy_one_shot_inventory_cannot_spawn_from_a_paused_receipt(self):
        plan = self.rearm_fixture()
        state = plan['move_state']
        state.pop('subscriptions')
        state['re_arms'][0]['argv'].remove('--persistent')
        write_json(Path(plan['move_state_file']), state)
        with patch.object(lanemove.subprocess, 'Popen') as spawn, self.assertRaisesRegex(
                lanemove.Refusal, 'invalid_bus_waiter'):
            lanemove.rearm_all(self.manifest, plan)
        spawn.assert_not_called()

    def test_broadened_ref_roots_cannot_hide_an_existing_private_move(self):
        self.invoke('--plan')
        self.manifest.mcp.ref_roots = [str(self.manifest.paths.root)]
        with self.assertRaisesRegex(lanemove.Refusal, 'exposed_legacy_receipts'):
            lanemove.prepare(self.manifest, 'worker', str(self.target))

    def test_required_waiter_refuses_every_finite_timeout_syntax(self):
        self.frozen()
        plan = self.plan()
        path = Path(plan['move_state_file'])
        state = read_json(path)
        argv = state['re_arms'][0]['argv']
        pos = argv.index('--timeout')
        base = argv[:pos] + argv[pos+2:]
        for suffix in (['--timeout', '1'], ['--timeout=1'], ['--timeout', '0', '--timeout', '2']):
            with self.subTest(suffix=suffix):
                state['re_arms'][0]['argv'] = base + suffix
                write_json(path, state)
                with self.assertRaisesRegex(lanemove.Refusal, 'unbounded_lane_waiter_required'):
                    lanemove.final_state(self.manifest, plan)
        state['re_arms'][0]['argv'] = base
        write_json(path, state)
        self.assertEqual(lanemove.lane_waiter(lanemove.final_state(self.manifest, plan), 'worker')['argv'], base)

    def test_required_waiter_refuses_narrowed_kinds_and_disabled_own_asks(self):
        plan = self.rearm_fixture()
        path = Path(plan['move_state_file'])
        state = read_json(path)
        base = state['re_arms'][0]['argv']
        for suffix in (['--kinds', 'fyi'], ['--kinds=ask,handoff'],
                       ['--kinds', 'ask,handoff,ruling,fail,apply_request,blocker,decision', '--kinds=fyi'],
                       ['--no-own-asks']):
            with self.subTest(suffix=suffix):
                state['re_arms'][0]['argv'] = base + suffix
                write_json(path, state)
                with self.assertRaisesRegex(lanemove.Refusal, 'complete_lane_waiter_required'):
                    lanemove.final_state(self.manifest, plan)
        # Own-ask replies are on by default; only additional waiters may narrow.
        state['re_arms'][0]['argv'] = [arg for arg in base if arg != '--own-asks']
        state['re_arms'].append(dict(name='extra', kind='bus-waiter', argv=[
            'coordkit', 'bus', 'wait', '--to=other', '--persistent', '--kinds=fyi', '--no-own-asks']))
        write_json(path, state)
        lanemove.final_state(self.manifest, plan)

    def test_equals_target_requires_fresh_cursor_and_matching_owner(self):
        plan = self.rearm_fixture()
        path = Path(plan['move_state_file'])
        state = read_json(path)
        entry = state['re_arms'][0]
        pos = entry['argv'].index('--to')
        entry['argv'][pos:pos+2] = ['--to=worker']
        write_json(path, state)
        lanemove.final_state(self.manifest, plan)
        outcome = dict(pid=55, started='new', requested_at=time.time()-10)
        snapshot = {55: dict(started='new', argv=shlex.join(entry['argv']))}
        verify = lambda: lanemove.verify_rearm(self.manifest, plan, entry, outcome, snapshot)
        self.assertFalse(verify())  # Live argv alone is never readiness.
        cursor = self.manifest.paths.bus_log.parent/'state/cursors/worker.wait'
        cursor.parent.mkdir(parents=True, exist_ok=True)
        cursor.write_text('0')
        self.assertFalse(verify())
        owner_path = cursor.with_suffix('.wait.owner.json')
        owner = dict(pid=55, started='new', session_id=plan['after']['session_id'])
        for wrong in (dict(pid=56), dict(started='old'), dict(session_id='old-session')):
            write_json(owner_path, dict(owner, **wrong))
            self.assertFalse(verify())
        write_json(owner_path, owner)
        os.utime(cursor, (1, 1))
        self.assertFalse(verify())
        os.utime(cursor, None)
        self.assertTrue(verify())

    def test_equals_target_cannot_bypass_duplicate_waiter_detection(self):
        plan = self.rearm_fixture()
        entry = plan['move_state']['re_arms'][0]
        other = list(entry['argv'])
        pos = other.index('--to')
        other[pos:pos+2] = ['--to=worker']
        snapshot = {55: dict(started='old', argv=shlex.join(other))}
        self.assertTrue(lanemove.duplicate_waiter(self.manifest, entry, snapshot))
        entry['argv'] = other
        with patch.object(claude_resume, 'processes', return_value=snapshot), self.assertRaisesRegex(
                lanemove.Refusal, 'duplicate_waiter'):
            lanemove.waiter_available(self.manifest, entry)

    def test_completed_move_recovers_dead_waiter_once_without_replaying_other_arms(self):
        self.frozen()
        self.complete()
        plan = self.plan()
        plan['rearm_outcomes'] = dict(worker=dict(pid=44, started='old', requested_at=1, status='verified'))
        lanemove.save(plan)
        self.rearm_patch.stop()
        snapshot = {}
        def spawn(argv, **kwargs):
            snapshot[55] = dict(started='new', argv=shlex.join(argv))
            cursor = self.manifest.paths.bus_log.parent/'state/cursors/worker.wait'
            cursor.parent.mkdir(parents=True, exist_ok=True)
            cursor.write_text('0')
            write_json(cursor.with_suffix('.wait.owner.json'), dict(pid=55, started='new', session_id='session-one'))
            return Mock(pid=55)
        rows = len(self.rows())
        with patch.object(claude_resume, 'processes', return_value=snapshot), patch.object(
                claude_resume, 'start_time', return_value='new'), patch.object(
                lanemove.subprocess, 'Popen', side_effect=spawn) as start:
            self.assertEqual(self.invoke('--apply')[0], 0)
            self.assertEqual(self.invoke('--apply')[0], 0)
        start.assert_called_once()
        self.assertEqual(self.plan()['waiter_restart_count'], 1)
        self.assertEqual(self.plan()['rearm_outcomes']['worker']['pid'], 55)
        self.assertEqual(len(self.rows()), rows)

    def test_completed_move_refuses_ambiguous_waiter_instead_of_duplicating(self):
        self.frozen()
        self.complete()
        plan = self.plan()
        plan['rearm_outcomes'] = dict(worker=dict(status='intent', requested_at=1))
        lanemove.save(plan)
        with patch.object(lanemove.subprocess, 'Popen') as start, self.assertRaisesRegex(
                lanemove.Refusal, 'waiter_recovery_ambiguous'):
            self.invoke('--apply')
        start.assert_not_called()

    def test_obligation_tail_is_bounded_and_refuses_incomplete_history(self):
        self.invoke('--plan')
        self.manifest.paths.bus_log.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.paths.bus_log.write_text('{}\n'*100)
        with patch.object(lanemove, 'MAX_OBLIGATION_BYTES', 16), patch.object(
                bus, 'read', side_effect=AssertionError('full bus reads forbidden')), self.assertRaisesRegex(
                lanemove.Refusal, 'obligation_history_limit'):
            lanemove.open_work(self.manifest, self.plan())

    def test_transcript_copy_streams_without_read_bytes(self):
        self.invoke('--plan')
        plan = self.plan()
        with self.source.open('wb') as stream:
            for _ in range(4):
                stream.write(b'x'*(1024*1024))
        real = Path.read_bytes
        def read(path):
            if path == self.source:
                raise AssertionError('transcript must be streamed')
            return real(path)
        with patch.object(Path, 'read_bytes', read), patch.object(
                lanemove.shutil, 'copyfileobj', wraps=lanemove.shutil.copyfileobj) as copy:
            lanemove.copy_session(plan)
        self.assertEqual(copy.call_args.kwargs['length'], 1024*1024)
        destination = Path(plan['destination'])
        self.assertEqual(destination.stat().st_size, 4*1024*1024)
        self.assertEqual(lanemove.checksum(destination), lanemove.checksum(self.source))
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_distinct_subscription_filters_are_all_recreated_and_verified(self):
        plan = self.rearm_fixture()
        path = Path(plan['move_state_file'])
        state = read_json(path)
        state['subscriptions'] = [dict(execution_id='exec-example', source_host='source', target='/old/events',
                                       expires_at='2020-01-01T00:00:00Z', kinds=[kind])
                                  for kind in ('terminal', 'attention')]
        write_json(path, state)
        lanemove.final_state(self.manifest, plan)
        fresh = [dict(entry, source_host='destination', target=str(self.manifest.paths.events),
                      subscription_id='new-'+entry['kinds'][0], state='active', expires_at='2999-01-01T00:00:00Z')
                 for entry in state['subscriptions']]
        with patch.object(lanemove.socket, 'gethostname', return_value='destination'), patch.object(
                lanemove.AgentctlClient, 'subscribe', side_effect=lambda execution, kinds: dict(id='new-'+kinds[0])) as create, patch.object(
                lanemove.AgentctlClient, 'lane_subscriptions', return_value=fresh):
            self.assertTrue(lanemove.rearm_subscriptions(self.manifest, plan, False))
            self.assertTrue(lanemove.rearm_subscriptions(self.manifest, plan, False))
        self.assertEqual(create.call_count, 2)
        self.assertEqual(len(plan['subscription_outcomes']), 2)
        self.assertTrue(all(o['verified'] for o in plan['subscription_outcomes'].values()))
