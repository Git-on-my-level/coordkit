"""Stop decisions use unread delivery rows and actual waiter evidence only."""
import builtins
import contextlib
import copy
import datetime as dt
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from unittest.mock import patch

from coordkit import addressed, deliver, hooks, hook_runtime, stop_agentctl, wake
from coordkit.adapters.claude_hook import load as hook_manifest
from coordkit.cli import main
from coordkit.manifest import ManifestError, load
from coordkit.state import read_json, write_json
from tests.test_deliver import DeliveryCase


class StopGuardTests(DeliveryCase):
    def setUp(self):
        super().setUp()
        # Never inspect the developer's journal, including tests using the CLI projection.
        inventory = patch.object(stop_agentctl.process, 'call_json', return_value={'executions': []})
        self.inventory = inventory.start()
        self.addCleanup(inventory.stop)

    def execution(self, state='completed'):
        return dict(id='exec-one-two-three-four-five-six', state=state,
                    labels=[stop_agentctl.session_label('s1')], unreconciled=True,
                    created_at=dt.datetime.now(dt.timezone.utc).isoformat())

    def work(self, *items):
        self.manifest.deliver.stop_requires_waiter = False
        self.inventory.return_value = {'executions': list(items)}

    def test_uncollected_terminal_work_blocks_and_collection_clears(self):
        for state in stop_agentctl.TERMINAL:
            with self.subTest(state=state):
                self.counter().unlink(missing_ok=True)
                item = self.execution(state)
                self.work(item)
                self.assertIn('agentctl result ' + item['id'] + ' --content', self.decision()['reason'])
                item['unreconciled'] = False
                self.assertIsNone(self.decision())
        argv = self.inventory.call_args.args[0]
        self.assertEqual(argv[:2], [os.environ['AGENTCTL'], 'recent'])
        self.assertIn(stop_agentctl.session_label('s1'), argv)
        self.assertIn('--since', argv)
        self.assertEqual(self.inventory.call_args.kwargs['retries'], 0)
        self.assertLessEqual(self.inventory.call_args.kwargs['timeout'], stop_agentctl.TIMEOUT)

    def test_running_work_requires_live_content_waiter_for_exact_execution(self):
        item = self.execution('running')
        self.work(item)
        with patch.object(stop_agentctl.process, 'run') as probe:
            probe.return_value = subprocess.CompletedProcess([], 0,
                f"S agentctl await {item['id']} --content\n", '')
            self.assertIsNone(self.decision())
            probe.return_value.stdout = f"S sh -c 'agentctl await {item['id']} --content'\n"
            self.assertIn('agentctl await ' + item['id'] + ' --content', self.decision()['reason'])
            probe.return_value.stdout = f"S agentctl await {item['id']}\n"
            self.assertIsNotNone(self.decision())
            self.assertIsNone(self.decision(), 'work uses the existing two-block budget')

    def test_executable_override_and_detached_full_width_process_snapshot(self):
        item = self.execution('running')
        self.work(item)
        with patch.dict(os.environ, AGENTCTL='/tools/custom-agentctl'), patch.object(
                stop_agentctl.process, 'run', return_value=subprocess.CompletedProcess(
                    [], 0, f"S /tools/custom-agentctl await {item['id']} --content", '')) as probe:
            self.assertIsNone(self.decision())
            self.assertEqual(self.inventory.call_args.args[0][0], '/tools/custom-agentctl')
            self.assertIn('-ww', probe.call_args.args[0])
            self.assertIn('-x', probe.call_args.args[0])
            item['state'] = 'completed'
            self.assertIn('/tools/custom-agentctl result', self.decision()['reason'])

    def test_waiter_snapshot_rejects_stopped_zombie_and_unrelated_commands(self):
        key = self.execution()['id']
        lines = [f'Z agentctl await {key} --content', f'T agentctl await {key} --content',
                 f'S agentctl result {key} --content', f'S agentctl delegate --label await {key} --content',
                 f'S agentctl await {key} --content --no-acknowledge',
                 f'S agentctl --journal alternate.db await {key} --content']
        with patch.object(stop_agentctl.process, 'run', return_value=subprocess.CompletedProcess(
                [], 0, '\n'.join(lines), '')):
            self.assertEqual(stop_agentctl.live_waiters(.1), set())
        with patch.object(stop_agentctl.process, 'run', return_value=subprocess.CompletedProcess(
                [], 0, f'S agentctl --output json await {key} --content', '')):
            self.assertEqual(stop_agentctl.live_waiters(.1), {key})

    def test_agentctl_timeout_busy_missing_malformed_and_probe_errors_fail_open(self):
        self.work(self.execution())
        for error in (stop_agentctl.process.ToolError('agentctl', 6, 'timeout', 'tool_timeout'),
                      stop_agentctl.process.ToolError('agentctl', 6, 'journal_busy'),
                      FileNotFoundError(), TimeoutError()):
            with self.subTest(error=error):
                self.inventory.side_effect = error
                self.assertIsNone(self.decision())
        self.inventory.side_effect = None
        for malformed in ({}, {'executions': None}, {'executions': [None]}):
            self.inventory.return_value = malformed
            self.assertIsNone(self.decision())
        self.work(self.execution('running'))
        with patch.object(stop_agentctl.process, 'run', side_effect=OSError('ps unavailable')):
            self.assertIsNone(self.decision())
        # Inventory errors never disable unread-bus enforcement.
        self.incumbent_row('pending', 'x', 'lead-a')
        self.assertIn('addressed bus rows', self.decision()['reason'])

    def test_outer_deadline_interrupts_slow_inventory_and_suppresses_output(self):
        self.work(self.execution())
        finished = []
        def slow_inventory(*args, **kwargs):
            time.sleep(1)
            finished.append(True)
        self.inventory.side_effect = slow_inventory
        with patch.object(deliver, 'HOOK_TIMEOUT', .03), patch.object(Path, 'unlink') as unlink:
            self.assertIsNone(self.decision())
            unlink.assert_not_called()
        self.assertEqual(finished, [])

    def test_agentctl_and_bus_reasons_share_budget(self):
        self.incumbent_row('pending', 'x', 'lead-a')
        self.assertIn('addressed bus rows', self.decision()['reason'])
        self.run_hook()
        self.assertIn('bus wait', self.decision()['reason'])
        self.work(self.execution())
        self.assertIsNone(self.decision())
        self.assertEqual(read_json(self.counter())['blocks'], 2)

    def test_agentctl_scope_never_uses_cwd_harness_or_child_session(self):
        item = self.execution()
        self.work(item)
        item['labels'] = ['coordkit.lane.lead-a']
        self.assertIsNone(self.decision())
        item['labels'] = [stop_agentctl.session_label('other-session')]
        self.assertIsNone(self.decision())
        item['labels'] = [stop_agentctl.session_label('s1')]
        item['created_at'] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=25)).isoformat()
        self.assertIsNone(self.decision())
        self.inventory.reset_mock()
        self.assertIsNone(stop_agentctl.block_reason(None))
        self.inventory.assert_not_called()

    def test_agentctl_reason_caps_ids_and_inventory_pages(self):
        items = [self.execution() for _ in range(7)]
        for word, item in zip(('one', 'two', 'three', 'four', 'five', 'six', 'seven'), items):
            item['id'] = f'exec-{word}-two-three-four-five-six'
        self.work(*items)
        reason = self.decision()['reason']
        self.assertEqual(reason.count('agentctl result'), 5)
        self.assertIn('+2 more', reason)
        self.inventory.return_value = {'executions': [], 'has_more': True, 'next_cursor': 'next'}
        self.inventory.reset_mock()
        self.assertIsNone(self.decision())
        self.assertEqual(self.inventory.call_count, stop_agentctl.MAX_PAGES)
        self.assertEqual(self.inventory.call_args.args[0][-2:], ['--cursor', 'next'])

    def test_collection_kill_switch_defaults_overrides_and_validation(self):
        key = 'stop_requires_agentctl_collection'
        self.assertTrue(getattr(self.manifest.deliver, key))
        self.assertTrue(getattr(hook_manifest(self.file).deliver, key))
        self.work(self.execution())
        setattr(self.manifest.deliver, key, False)
        self.assertIsNone(self.decision())
        self.inventory.assert_not_called()
        self.incumbent_row('pending', 'x', 'lead-a')
        self.assertIsNotNone(self.decision())
        base = self.file.read_text()
        lane = '\n[[lanes]]\nname="lead-a"\nconfig_dir="identity"\ncwd="."\nmodel="test"\neffort="test"\n'
        for global_value, override in (('true', 'false'), ('false', 'true')):
            self.file.write_text(base + '\n[deliver]\n' + key + '=' + global_value + '\n' + lane + key + '=' + override + '\n')
            for manifest in (load(self.file), hook_manifest(self.file)):
                self.assertEqual(deliver.lane_setting(manifest, 'lead-a', key), override == 'true')
                self.assertEqual(deliver.lane_setting(manifest, 'worker-ui', key), global_value == 'true')
        for bad in ('"false"', '1'):
            for data in (base + '\n[deliver]\n' + key + '=' + bad, base + lane + key + '=' + bad):
                self.file.write_text(data + '\n')
                for loader in (load, hook_manifest):
                    with self.assertRaises(ManifestError):
                        loader(self.file)

    def decision(self, **payload):
        out = io.StringIO()
        self.assertEqual(deliver.run_stop_hook(self.manifest, json.dumps(dict(
            self.payload, hook_event_name='Stop', **payload)), out), 0)
        return json.loads(out.getvalue()) if out.getvalue() else None

    def counter(self, session='s1'):
        return deliver.stop_counter_path(self.manifest, 'lead-a', session)

    def test_unread_filter_matches_delivery_without_advancing_cursor(self):
        for kind in ('ask', 'ruling', 'handoff', 'blocker'):
            self.incumbent_row(kind, 'x', 'lead-a', kind)
        self.incumbent_row('role', 'x', 'coordinator', 'msg', urgent=True)
        self.incumbent_row('owner', 'x', 'other', 'ask', owner='lead-a')
        self.incumbent_row('all', 'x', 'all', 'ask')
        self.incumbent_row('other', 'x', 'other', 'ask')
        self.incumbent_row('self', 'lead-a', 'lead-a', 'ask')
        self.incumbent_row('ignored', 'x', 'lead-a', 'fyi')
        path = deliver.cursor_path(self.manifest, 'lead-a')
        before = path.read_bytes()
        result = self.decision()
        self.assertEqual(result, {'decision': 'block', 'reason':
            '7 addressed bus rows are unread: read them with your bus inbox command, handle them, '
            'then end your turn with a background bus wait armed'})
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(deliver.note_path(self.manifest, 'lead-a').exists())

    def test_waiter_required_ignores_hook_transcript_and_legacy_lease(self):
        self.lease('lead-a')
        transcript = self.root/'active.jsonl'
        transcript.write_text('{}\n')
        write_json(deliver.note_path(self.manifest, 'lead-a'), dict(
            name='lead-a', at=time.time(), transcript_path=str(transcript), session_id='s1'))
        self.assertTrue(addressed.listening(self.manifest, 'lead-a')[0])
        self.assertFalse(addressed.listening(self.manifest, 'lead-a', waiter_only=True)[0])
        self.assertEqual(self.decision()['reason'],
            'arm a background `bus wait --to lead-a` (your usual waiter) before ending the turn '
            'so addressed work wakes you')

    def test_armed_lock_and_fresh_cursor_allow_stop_but_unread_wins(self):
        directory = self.manifest.paths.bus_log.parent/'state/cursors'
        lock = directory/'lead-a.wait.lock'
        with addressed.locked(lock):
            self.assertIsNone(self.decision())
            self.incumbent_row('pending', 'x', 'lead-a')
            self.assertIn('1 addressed bus rows', self.decision()['reason'])
        self.run_hook()  # only delivery consumes its cursor
        cursor = directory/'lead-a.wait'
        cursor.write_text('1\n')
        self.assertIsNone(self.decision())
        for age in (181, -100):
            os.utime(cursor, (time.time()-age, time.time()-age))
            self.assertEqual(self.decision()['decision'], 'block')
        cursor.write_text('not a cursor')
        self.counter().unlink()
        self.assertIsNotNone(self.decision())

    def test_configuration_false_only_disables_waiter_requirement(self):
        self.manifest.deliver.stop_requires_waiter = False
        self.assertIsNone(self.decision())
        self.incumbent_row('pending', 'x', 'lead-a')
        self.assertIsNotNone(self.decision())

    def test_wait_cursor_contract_uses_independent_freshness_window(self):
        directory = self.manifest.paths.bus_log.parent/'state/cursors'
        cursor = directory/'lead-a.wait'
        cursor.write_text('1\n')
        os.utime(cursor, (self.now - 20, self.now - 20))
        self.manifest.addressed.rearm_grace = '1s'
        self.manifest.deliver.waiter_fresh_seconds = 60
        self.assertIsNone(self.decision())
        self.manifest.addressed.rearm_grace = '1d'
        self.manifest.deliver.waiter_fresh_seconds = 10
        self.assertIsNotNone(self.decision())
        with addressed.locked(directory/'lead-a.wait.lock'):
            self.assertIsNone(self.decision())

    def test_lane_override_opt_out_keeps_unread_guard_and_other_lanes_required(self):
        lane = '\n[[lanes]]\nname="lead-a"\nconfig_dir="identity"\ncwd="."\nmodel="test"\neffort="test"\n'
        base = self.file.read_text()
        self.file.write_text(base + lane + 'stop_requires_waiter=false\n')
        self.manifest = load(self.file)
        projection = hook_manifest(self.file)
        self.assertFalse(deliver.requires_waiter(projection, 'lead-a'))
        self.assertTrue(deliver.requires_waiter(projection, 'worker-ui'))
        self.assertIsNone(self.decision())
        self.incumbent_row('pending', 'x', 'lead-a')
        self.assertIsNotNone(self.decision())
        for value in ('"false"', '1'):
            self.file.write_text(base + lane + 'stop_requires_waiter='+value+'\n')
            with self.assertRaises(ManifestError):
                load(self.file)
            with self.assertRaises(ManifestError):
                hook_manifest(self.file)

    def test_waiter_fresh_seconds_defaults_and_validation_match_projection(self):
        self.assertEqual(self.manifest.deliver.waiter_fresh_seconds, 180)
        base = self.file.read_text()
        for value in ('0', '42'):
            self.file.write_text(base + '\n[deliver]\nwaiter_fresh_seconds='+value+'\n')
            self.assertEqual(load(self.file).deliver.waiter_fresh_seconds, int(value))
            self.assertEqual(hook_manifest(self.file).deliver.waiter_fresh_seconds, int(value))
        for value in ('-1', 'true', '1.5', '"180"'):
            self.file.write_text(base + '\n[deliver]\nwaiter_fresh_seconds='+value+'\n')
            with self.assertRaises(ManifestError):
                load(self.file)
            with self.assertRaises(ManifestError):
                hook_manifest(self.file)

    def test_missing_session_uses_transcript_path_budget_without_sharing_lanes(self):
        for index in range(2):
            path = self.root/f'transcript-{index}.jsonl'
            path.write_text(json.dumps({'type': 'user', 'content': 'Your bus name is lead-a.'}))
            self.assertIsNotNone(self.decision(session_id=None, transcript_path=str(path)))
            self.assertIsNotNone(self.decision(session_id=None, transcript_path=str(path)))
            self.assertIsNone(self.decision(session_id=None, transcript_path=str(path)))
            counter = deliver.stop_counter_path(self.manifest, 'lead-a', None, str(path))
            self.assertEqual(read_json(counter)['blocks'], 2)
        self.assertEqual(deliver.stop_counter_path(self.manifest, 'lead-a', None, str(path)),
                         deliver.stop_counter_path(self.manifest, 'lead-a', None, str(path.parent/'.'/path.name)))

    def test_no_session_or_transcript_fails_open_even_for_registered_lane(self):
        with patch.dict(os.environ, COORDKIT_AS='lead-a'), patch.object(deliver, 'unread_batch') as read:
            for _ in range(3):
                self.assertIsNone(self.decision(session_id=None))
            self.incumbent_row('pending', 'x', 'lead-a')
            self.assertIsNone(self.decision(session_id=None))
            read.assert_not_called()
        self.assertIsNone(deliver.stop_counter_path(self.manifest, 'lead-a', None))
        self.assertEqual(list(deliver.cursor_path(self.manifest, 'lead-a').parent.glob('*.stop.json')), [])

    def test_cli_deadline_includes_delivery_import(self):
        completed = []
        actual_import = builtins.__import__
        def slow_import(name, *args, **kwargs):
            if name == 'deliver':
                time.sleep(1)
                completed.append(True)
            return actual_import(name, *args, **kwargs)
        out, err = io.StringIO(), io.StringIO()
        with patch.object(builtins, '__import__', side_effect=slow_import), \
                patch.object(hook_runtime, 'HOOK_TIMEOUT', .03), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(main(['--manifest', str(self.file), 'deliver', '--stop-hook']), 0)
        self.assertEqual(completed, [])
        self.assertEqual((out.getvalue(), err.getvalue()), ('', ''))

    def test_unknown_unregistered_and_retired_lanes_are_silent(self):
        self.assertIsNone(self.decision(session_id='unknown'))
        with patch.dict(os.environ, COORDKIT_AS='unregistered'):
            self.assertIsNone(self.decision())
        write_json(wake.state_path(self.manifest), {'lead-a': {'owner': 'lead'}})
        self.incumbent_row('pending', 'x', 'lead-a')
        self.assertIsNone(self.decision())
        self.assertFalse(self.counter().exists())

    def test_loop_guards_session_isolation_window_and_clean_stop_reset(self):
        self.assertIsNone(self.decision(stop_hook_active=True))
        self.assertFalse(self.counter().exists())
        self.assertIsNotNone(self.decision())
        self.assertIsNone(self.decision(stop_hook_active=True))
        self.assertIsNotNone(self.decision())
        self.assertIsNone(self.decision())
        self.assertEqual(read_json(self.counter())['blocks'], 2)
        with patch.dict(os.environ, COORDKIT_AS='lead-a'):
            self.assertIsNotNone(self.decision(session_id='another-session'))
        record = read_json(self.counter())
        with patch.object(deliver.time, 'time', return_value=record['at'] + 601):
            self.assertIsNotNone(self.decision())
        self.manifest.deliver.stop_requires_waiter = False
        self.assertIsNone(self.decision())
        self.assertFalse(self.counter().exists())
        self.manifest.deliver.stop_requires_waiter = True
        self.assertIsNotNone(self.decision())

    def test_fail_open_errors_and_deadline_and_nonblocking_counter_lock(self):
        for payload in ('broken', '[]', '{}'):
            out = io.StringIO()
            self.assertEqual(deliver.run_stop_hook(self.manifest, payload, out), 0)
            self.assertEqual(out.getvalue(), '')
        with patch.object(deliver, 'write_json', side_effect=OSError('read only')):
            self.assertIsNone(self.decision())
        with patch.object(deliver, 'unread_batch', side_effect=ValueError('corrupt cursor')):
            self.assertIsNone(self.decision())
        with addressed.locked(self.counter().with_suffix('.lock')):
            self.assertIsNone(self.decision())
        completed = []
        def slow_read(*args):
            time.sleep(1)
            completed.append(True)
        with patch.object(deliver, 'HOOK_TIMEOUT', .03), patch.object(
                deliver, 'unread_batch', side_effect=slow_read):
            self.assertIsNone(self.decision())
        self.assertEqual(completed, [], 'deadline must interrupt the read, not swallow a later error')
        self.counter().write_text('invalid json')
        self.assertIsNone(self.decision())
        self.counter().unlink()
        self.manifest.paths.bus_log.unlink()
        self.assertIsNone(self.decision())

    def test_stop_first_attachment_partial_and_bounded_batch(self):
        self.manifest.deliver.stop_requires_waiter = False
        self.incumbent_row('historical', 'x', 'lead-a')
        cursor = deliver.cursor_path(self.manifest, 'lead-a')
        cursor.unlink()
        self.assertIsNone(self.decision())
        self.assertFalse(cursor.exists())
        self.run_hook()  # attaches at EOF
        with self.manifest.paths.bus_log.open('ab') as stream:
            stream.write(b'broken\n[]\n{"kind":"ask"}\n{"id":"partial"')
        self.assertIsNone(self.decision())
        self.run_hook()
        self.manifest.paths.bus_log.write_text('')
        self.incumbent_row('new', 'x', 'lead-a')
        with patch.object(deliver, 'MAX_READ', 512):
            self.assertIn('1 addressed bus rows', self.decision()['reason'])

    def test_wrong_event_output_error_and_bounded_input_fail_open(self):
        out = io.StringIO()
        self.assertEqual(deliver.run_stop_hook(self.manifest, json.dumps(self.payload), out), 0)
        self.assertEqual(out.getvalue(), '')
        with patch.object(out, 'write', side_effect=OSError('broken pipe')):
            self.assertEqual(deliver.run_stop_hook(self.manifest, json.dumps(dict(
                self.payload, hook_event_name='Stop')), out), 0)
        out = io.StringIO()
        with patch.object(deliver, 'MAX_READ', 8):
            self.assertEqual(deliver.run_stop_hook(self.manifest, '{}' * 10, out), 0)
        self.assertEqual(out.getvalue(), '')

    def test_nested_deadline_does_not_extend_bootstrap_budget(self):
        previous = signal.getsignal(signal.SIGALRM)
        with deliver.hook_deadline():
            remaining = signal.getitimer(signal.ITIMER_REAL)[0]
            with deliver.hook_deadline(50):
                self.assertLessEqual(signal.getitimer(signal.ITIMER_REAL)[0], remaining)
        self.assertEqual(signal.getsignal(signal.SIGALRM), previous)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)

    def test_cli_default_stop_guard_resolves_projected_registered_lane(self):
        out, err = io.StringIO(), io.StringIO()
        with patch('sys.stdin', io.StringIO(json.dumps(dict(self.payload, hook_event_name='Stop')))), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(main(['--manifest', str(self.file), 'deliver', '--stop-hook']), 0)
        self.assertEqual(err.getvalue(), '')
        self.assertEqual(json.loads(out.getvalue())['decision'], 'block')
        self.assertIn('bus wait --to lead-a', json.loads(out.getvalue())['reason'])

    def test_cli_projection_option_and_missing_manifest_fail_open(self):
        with self.file.open('a') as stream:
            stream.write('\n[deliver]\nstop_requires_waiter = false\n')
        self.assertFalse(load(self.file).deliver.stop_requires_waiter)
        self.assertFalse(hook_manifest(self.file).deliver.stop_requires_waiter)
        for path in (self.file, self.root/'missing.toml'):
            out, err = io.StringIO(), io.StringIO()
            with patch('sys.stdin', io.StringIO('{"hook_event_name":"Stop","session_id":"s1"}')), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(main(['--manifest', str(path), 'deliver', '--stop-hook']), 0)
            self.assertEqual((out.getvalue(), err.getvalue()), ('', ''))
        self.file.write_text(self.file.read_text().replace('stop_requires_waiter = false', 'stop_requires_waiter = "false"'))
        with self.assertRaisesRegex(ManifestError, 'deliver.stop_requires_waiter'):
            load(self.file)


class StopInstallTests(DeliveryCase):
    def test_stop_install_round_trip_mode_and_unrelated_settings(self):
        target = self.root/'settings.json'
        before = {'model': 'original', 'hooks': {'Stop': [
            {'hooks': [{'type': 'command', 'command': 'existing-stop'}]}]}}
        write_json(target, before)
        target.chmod(0o640)
        hooks.main(['install', '--settings', str(target)], self.manifest)
        installed = read_json(target)
        stop = installed['hooks']['Stop'][-1]['hooks'][0]['command']
        self.assertIn('COORDKIT_HOOK_OWNER=coordkit-delivery', stop)
        self.assertIn(' deliver --stop-hook', stop)
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        installed['model'] = 'unrelated edit'
        write_json(target, installed)
        deliver.uninstall(self.manifest, target)
        self.assertEqual(hooks.entries(read_json(target)), hooks.entries(before))
        self.assertEqual(read_json(target)['model'], 'unrelated edit')
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)

    def test_legacy_delivery_receipt_gains_stop_and_rolls_back_original(self):
        for via_install in (True, False):
            with self.subTest(via_install=via_install):
                target = self.root/f'settings-{via_install}.json'
                before = {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'original-stop'}]}]}}
                write_json(target, before)
                with patch.object(deliver, 'HOOK_EVENTS', deliver.EVENTS):
                    deliver.install(self.manifest, target)
                receipt = deliver.receipt_path(self.manifest, target)
                original_before = copy.deepcopy(read_json(receipt)['before'])
                current = read_json(target)
                current['theme'] = 'user edit'
                write_json(target, current)
                (deliver.install(self.manifest, target) if via_install else hooks.reconcile(self.manifest))
                deliver.install(self.manifest, target)
                self.assertEqual(len(read_json(target)['hooks']['Stop']), 2)
                self.assertEqual(read_json(receipt)['before'], original_before)
                deliver.uninstall(self.manifest, target)
                self.assertEqual(hooks.entries(read_json(target)), hooks.entries(before))
                self.assertEqual(read_json(target)['theme'], 'user edit')


    def test_stop_migration_recovers_interrupted_receipt(self):
        for applied in (False, True):
            target = self.root/f'interrupted-{applied}.json'
            with patch.object(deliver, 'HOOK_EVENTS', deliver.EVENTS):
                deliver.install(self.manifest, target)
            receipt = deliver.receipt_path(self.manifest, target)
            original = read_json(receipt)['before']
            actual_write = hooks.write_json
            def interrupt(path, doc):
                actual_write(path, doc)
                if path == receipt and doc.get('pending_after'):
                    if applied:
                        write_json(target, doc['pending_after'])
                    raise OSError('interrupted migration')
            with patch.object(hooks, 'write_json', side_effect=interrupt):
                with self.assertRaises(OSError):
                    hooks.reconcile(self.manifest)
            hooks.reconcile(self.manifest)
            self.assertEqual(len(read_json(target)['hooks']['Stop']), 1)
            self.assertNotIn('pending_after', read_json(receipt))
            deliver.uninstall(self.manifest, target)
            self.assertEqual(read_json(target), original)

    def test_reconcile_keeps_event_boundaries_and_unrelated_matchers(self):
        target = self.root/'event-settings.json'
        with patch('coordkit.paths.install_root', return_value=self.root/'old'):
            deliver.install(self.manifest, target)
        current = read_json(target)
        # Same command in a different matcher is outside this receipt's ownership.
        unrelated = {'matcher': 'custom', 'hooks': [copy.deepcopy(current['hooks']['Stop'][0]['hooks'][0])]}
        current['hooks']['Stop'].append(unrelated)
        write_json(target, current)
        with patch('coordkit.paths.install_root', return_value=self.root/'new'):
            hooks.reconcile(self.manifest)
            migrated = read_json(target)
            for event in deliver.HOOK_EVENTS:
                self.assertEqual(migrated['hooks'][event][0]['hooks'][0]['command'], deliver.command(self.manifest, event))
        self.assertEqual(migrated['hooks']['Stop'][-1], unrelated)

    def test_uninstall_missing_target_never_creates_settings(self):
        target = self.root/'missing-settings.json'
        deliver.install(self.manifest, target)
        target.unlink()
        deliver.uninstall(self.manifest, target)
        self.assertFalse(target.exists())
        self.assertTrue(read_json(deliver.receipt_path(self.manifest, target))['restored'])
        self.assertFalse(list(target.parent.glob(target.name+'.*.tmp')))

    def test_uninstall_target_deleted_during_receipt_window_stays_missing(self):
        target = self.root/'deleted-settings.json'
        deliver.install(self.manifest, target)
        actual_update = deliver.update_json
        def update(path, transform, **kwargs):
            prepare = kwargs['before_replace']
            def deleted(before, after):
                prepare(before, after)
                target.unlink()
            return actual_update(path, transform, **dict(kwargs, before_replace=deleted))
        with patch.object(deliver, 'update_json', side_effect=update):
            deliver.uninstall(self.manifest, target)
        self.assertFalse(target.exists())
        self.assertTrue(read_json(deliver.receipt_path(self.manifest, target))['restored'])

    def test_install_completes_receipt_written_before_settings_rename(self):
        target = self.root/'interrupted-install.json'
        before = {'model': 'original', 'hooks': {'Stop': [
            {'hooks': [{'type': 'command', 'command': 'original-stop'}]}]}}
        write_json(target, before)
        target.chmod(0o640)
        receipt = deliver.receipt_path(self.manifest, target)
        actual_write = deliver.write_json
        def interrupt(path, doc):
            actual_write(path, doc)
            if path == receipt:
                raise OSError('crash before settings rename')
        with patch.object(deliver, 'write_json', side_effect=interrupt), self.assertRaises(OSError):
            deliver.install(self.manifest, target)
        self.assertEqual(read_json(target), before)
        record = read_json(receipt)
        deliver.install(self.manifest, target)
        self.assertEqual(read_json(target), record['after'])
        self.assertEqual(read_json(receipt)['before'], before)
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        deliver.uninstall(self.manifest, target)
        self.assertEqual(read_json(target), before)
