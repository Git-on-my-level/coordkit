"""Caller binding uses native hook input, never transcripts or external tools."""
import contextlib
import copy
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from coordkit import deliver, hook_runtime, hooks, identity, session_start
from coordkit.adapters.claude_hook import load as hook_manifest
from coordkit.cli import main
from coordkit.manifest import load
from coordkit.state import read_json, write_json
from tests.support import BASE, FakeCase


class SessionStartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = self.root/'config'
        self.config.mkdir(mode=0o700)
        self.file = self.root/'coordkit.toml'
        self.file.write_text(BASE + '\n[[lanes]]\nname="lane"\nconfig_dir="config"\n'
                             'cwd="."\nmodel="test"\neffort="test"\nsession_id="s1"\n')
        self.manifest = load(self.file)
        self.payload = {'hook_event_name': 'SessionStart', 'session_id': 's1', 'source': 'startup'}
        env = patch.dict(os.environ, CLAUDE_CONFIG_DIR=str(self.config))
        env.start()
        self.addCleanup(env.stop)
        for key in (*session_start.MANAGED, 'AGENTCTL_CALLER_PROVIDER', 'AGENTCTL_CALLER_NATIVE_SESSION_ID'):
            os.environ.pop(key, None)
        self.target = self.target_for('s1')

    def target_for(self, session):
        path = self.config/'session-env'/session/'hook.sh'
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        os.environ['CLAUDE_ENV_FILE'] = str(path)
        return path

    def run_hook(self, payload=None, raw=None, manifest=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(session_start.run(manifest or self.manifest, raw if raw is not None else
                                              json.dumps(self.payload if payload is None else payload)), 0)
        self.assertEqual((out.getvalue(), err.getvalue()), ('', ''))

    def consume(self, target, session):
        fake = self.root/'fake-agentctl.py'
        fake.write_text('import os,json\nprint(json.dumps({"provider_matches":'
                        'os.environ.get("AGENTCTL_CALLER_PROVIDER")=="claude",'
                        '"session_matches":os.environ.get("AGENTCTL_CALLER_NATIVE_SESSION_ID")==os.environ["EXPECTED"]}))\n')
        # Manual sourcing verifies generated shell data, not a model-driven Bash turn.
        result = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-c',
                                 '. "$1"; exec "$2" "$3"', 'probe', str(target), sys.executable, str(fake)],
                                cwd=self.root, env={'PATH': os.defpath, 'EXPECTED': session},
                                capture_output=True, text=True, timeout=3, check=True)
        self.assertEqual(result.stderr, '')
        self.assertEqual(json.loads(result.stdout), {'provider_matches': True, 'session_matches': True})

    def test_pin_creates_private_exports_once_and_preserves_other_hooks(self):
        # Native 2.1.322 supplies a path whose leaf does not exist yet.
        self.run_hook()
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
        original = self.target.read_bytes()
        self.run_hook()
        self.assertEqual(self.target.read_bytes(), original)
        self.consume(self.target, 's1')
        plugin = b'export PLUGIN_VALUE=retained'  # no final newline
        self.target.write_bytes(plugin)
        self.run_hook()
        self.assertTrue(self.target.read_bytes().startswith(plugin + b'\n'))
        self.consume(self.target, 's1')

    def test_saved_binding_for_known_unpinned_claude_and_two_sessions_isolate(self):
        self.manifest.lanes[0].session_id = ''
        write_json(identity.home(self.manifest)/'sessions.json', {'s1': {'name': 'lane'}, 's2': {'name': 'lane'}})
        self.run_hook()
        second = self.target_for('s2')
        self.run_hook(dict(self.payload, session_id='s2', source='resume'))
        self.consume(self.target, 's1')
        self.consume(second, 's2')
        self.assertNotEqual(self.target.read_bytes(), second.read_bytes())

    def check_claude_code_alias(self, bound):
        raw = self.file.read_text() + 'harness="claude-code"\n'
        self.file.write_text(raw.replace('session_id="s1"', 'session_id=""') if bound else raw)
        if bound:
            write_json(identity.home(load(self.file))/'sessions.json', {'s1': {'name': 'lane'}})
        # Both supported manifest readers must keep the established Claude alias
        # in scope; silently dropping it loses attribution for future children.
        for reader in (load, hook_manifest):
            with self.subTest(reader=reader.__module__):
                self.target.unlink(missing_ok=True)
                self.run_hook(manifest=reader(self.file))
                self.assertTrue(self.target.is_file())
                self.consume(self.target, 's1')

    def test_claude_code_alias_pin_binds(self):
        self.check_claude_code_alias(False)

    def test_claude_code_alias_saved_binding_binds(self):
        self.check_claude_code_alias(True)

    def test_conflicting_pins_and_saved_binding_never_choose_a_lane(self):
        other = copy.copy(self.manifest.lanes[0])
        other.name = 'other'
        self.manifest.lanes.append(other)
        self.run_hook()
        self.assertFalse(self.target.exists())
        other.session_id = ''
        write_json(identity.home(self.manifest)/'sessions.json', {'s1': {'name': 'other'}})
        self.run_hook()
        self.assertFalse(self.target.exists())

    def test_managed_or_ambiguous_markers_never_override_parent_identity(self):
        for key in session_start.MANAGED:
            for value in ('managed-value', ''):
                with self.subTest(key=key, value=value), patch.dict(os.environ, {key: value,
                        'AGENTCTL_CALLER_PROVIDER': 'other', 'AGENTCTL_CALLER_NATIVE_SESSION_ID': 'original'}):
                    tracked = (*session_start.MANAGED, 'AGENTCTL_CALLER_PROVIDER', 'AGENTCTL_CALLER_NATIVE_SESSION_ID')
                    before = {name: os.environ.get(name) for name in tracked}
                    self.run_hook()
                    self.assertFalse(self.target.exists())
                    self.assertEqual({name: os.environ.get(name) for name in tracked}, before)
        with patch.dict(os.environ, AGENTCTL_EXECUTION_ID='inherited', AGENTCTL_ADAPTER='codex', CLAUDECODE='1'):
            self.run_hook()
            self.assertFalse(self.target.exists())

    def test_persisted_file_cannot_override_managed_markers_when_sourced_later(self):
        self.run_hook()
        for key in session_start.MANAGED:
            for value in ('managed-value', ''):
                with self.subTest(key=key, empty=not value):
                    result = subprocess.run(['/bin/bash', '--noprofile', '--norc', '-c',
                                             '. "$1"; test "$AGENTCTL_CALLER_PROVIDER" = original && '
                                             'test "$AGENTCTL_CALLER_NATIVE_SESSION_ID" = original',
                                             'probe', str(self.target)], env={'PATH': os.defpath, key: value,
                                             'AGENTCTL_CALLER_PROVIDER': 'original',
                                             'AGENTCTL_CALLER_NATIVE_SESSION_ID': 'original'},
                                            capture_output=True, timeout=3)
                    self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b'', b''))

    def test_unknown_scope_ignores_ambient_name_cwd_and_transcript(self):
        self.manifest.lanes[0].session_id = ''
        with patch.dict(os.environ, COORDKIT_AS='lane'), patch.object(identity, 'asker_for', side_effect=AssertionError):
            self.run_hook(dict(self.payload, cwd=str(self.root), transcript_path='/never-read'))
        self.assertFalse(self.target.exists())
        for name in ('missing', 'lane'):
            write_json(identity.home(self.manifest)/'sessions.json', {'s1': {'name': name}})
            self.manifest.lanes[0].harness = 'opencode'
            self.run_hook()
            self.assertFalse(self.target.exists())

    def test_conflicting_binding_or_pin_and_malformed_binding_fail_open(self):
        for record in ({'s1': {'name': 'missing'}}, {'s1': None}, [], {'s1': {}}):
            write_json(identity.home(self.manifest)/'sessions.json', record)
            self.run_hook()
            self.assertFalse(self.target.exists())
        write_json(identity.home(self.manifest)/'sessions.json', {'s2': {'name': 'lane'}})
        self.target_for('s2')
        self.run_hook(dict(self.payload, session_id='s2'))
        self.assertFalse((self.config/'session-env/s2/hook.sh').exists())

    def test_collection_switches_on_full_manifest_and_hook_projection(self):
        for global_value, override, expected in ((False, None, False), (False, True, True), (True, False, False)):
            with self.subTest(global_value=global_value, override=override):
                base = self.file.read_text().split('\n[deliver]')[0]
                base = base.replace('\nstop_requires_agentctl_collection=true', '').replace(
                    '\nstop_requires_agentctl_collection=false', '')
                if override is not None:
                    base += '\nstop_requires_agentctl_collection=' + str(override).lower() + '\n'
                self.file.write_text(base + '\n[deliver]\nstop_requires_agentctl_collection=' + str(global_value).lower())
                for loader in (load, hook_manifest):
                    self.target.unlink(missing_ok=True)
                    self.run_hook(manifest=loader(self.file))
                    self.assertEqual(self.target.exists(), expected)

    def test_invalid_utf8_controls_surrogates_and_oversize_fail_open(self):
        for raw in (b'\xff', b'{}' + b' ' * session_start.MAX_INPUT, b'[]', b'null', b'{',
                    json.dumps(dict(self.payload, session_id='a\x00b')),
                    json.dumps(dict(self.payload, session_id='a\x85b')),
                    json.dumps(dict(self.payload, session_id='\ud800')),
                    json.dumps(dict(self.payload, session_id='é' * 129)),
                    json.dumps(dict(self.payload, session_id=' ')),
                    json.dumps(dict(self.payload, session_id=123))):
            with self.subTest(raw=repr(raw)[:60]):
                self.run_hook(raw=raw)
                self.assertFalse(self.target.exists())

    def test_shell_metacharacters_are_literal_session_data(self):
        session = "s'$(touch INJECTED);`echo bad`"
        self.manifest.lanes[0].session_id = ''
        write_json(identity.home(self.manifest)/'sessions.json', {session: {'name': 'lane'}})
        target = self.target_for(session)
        self.run_hook(dict(self.payload, session_id=session))
        self.consume(target, session)
        self.assertFalse((self.root/'INJECTED').exists())

    def test_missing_handle_parent_and_outside_or_wrong_session_targets_skip(self):
        for target in ('', 'relative.sh', str(self.root/'outside.sh'),
                       str(self.config/'session-env/missing/hook.sh'), str(self.config/'settings.json')):
            with self.subTest(target=target), patch.dict(os.environ, CLAUDE_ENV_FILE=target):
                self.run_hook()
                self.assertFalse(self.target.exists())
                self.assertFalse((self.root/'outside.sh').exists())
        with patch.dict(os.environ, CLAUDE_ENV_FILE=str(self.target.parent/'absent/hook.sh')):
            self.run_hook()
            self.assertFalse(self.target.exists())

    def test_nonprivate_unwritable_wrong_owner_hardlink_and_fifo_skip(self):
        for mode in (0o400, 0o644, 0o666, 0o700):
            self.target.unlink(missing_ok=True)
            self.target.write_bytes(b'original')
            self.target.chmod(mode)
            self.run_hook()
            self.assertEqual(self.target.read_bytes(), b'original')
        self.target.chmod(0o600)
        with patch.object(session_start.os, 'getuid', return_value=os.getuid() + 1):
            self.run_hook()
        self.assertEqual(self.target.read_bytes(), b'original')
        hardlink = self.target.with_name('hardlink')
        os.link(self.target, hardlink)
        self.run_hook()
        self.assertEqual(hardlink.read_bytes(), b'original')
        self.target.unlink()
        os.mkfifo(self.target, 0o600)
        self.run_hook()

    def test_symlink_leaf_directory_and_config_ancestor_never_followed(self):
        other = self.root/'other'
        other.mkdir(mode=0o700)
        victim = other/'victim'
        victim.write_bytes(b'private')
        victim.chmod(0o600)
        self.target.symlink_to(victim)
        self.run_hook()
        self.assertEqual(victim.read_bytes(), b'private')
        self.target.unlink()
        self.target.parent.rmdir()
        self.target.parent.symlink_to(other, target_is_directory=True)
        self.run_hook()
        self.assertFalse((other/'hook.sh').exists())
        self.target.parent.unlink()
        self.target.parent.mkdir(mode=0o777)
        self.target.parent.chmod(0o777)
        self.run_hook()
        self.assertFalse(self.target.exists())
        self.target.parent.chmod(0o700)
        alias = self.root/'alias'
        alias.symlink_to(self.config, target_is_directory=True)
        self.manifest.lanes[0].config_dir = str(alias)
        with patch.dict(os.environ, CLAUDE_ENV_FILE=str(alias/'session-env/s1/hook.sh')):
            self.run_hook()
        self.assertFalse(self.target.exists())

    def test_file_and_append_bounds_and_nonblocking_lock(self):
        self.run_hook()
        block_size = self.target.stat().st_size
        for size, changes in ((session_start.MAX_ENV - block_size, True),
                              (session_start.MAX_ENV - block_size + 1, False),
                              (session_start.MAX_ENV + 1, False)):
            self.target.write_bytes(b'#' * size)
            self.run_hook()
            self.assertEqual(self.target.stat().st_size, size + (block_size if changes else 0))
        self.target.write_bytes(b'unchanged')
        with self.target.open('rb') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.run_hook()
        self.assertEqual(self.target.read_bytes(), b'unchanged')

    def test_short_append_or_write_error_preserves_existing_contents(self):
        self.target.write_bytes(b'original')
        self.target.chmod(0o600)
        original = os.write
        with patch.object(session_start.os, 'write', side_effect=lambda fd, data: original(fd, data[:7])):
            self.run_hook()
        self.assertEqual(self.target.read_bytes(), b'original')
        with patch.object(session_start.os, 'write', side_effect=OSError):
            self.run_hook()
        self.assertEqual(self.target.read_bytes(), b'original')

    def test_core_deadline_skips_without_a_declaration(self):
        with patch.object(session_start, 'TIMEOUT', .02), patch.object(
                session_start, 'scoped_lane', side_effect=lambda *args: time.sleep(1)):
            before = time.monotonic()
            self.run_hook()
            self.assertLess(time.monotonic() - before, .5)
        self.assertFalse(self.target.exists())

    def test_oversized_binding_and_readonly_errors_fail_open(self):
        path = identity.home(self.manifest)/'sessions.json'
        path.parent.mkdir(parents=True)
        path.write_bytes(b' ' * (session_start.MAX_BINDINGS + 1))
        self.run_hook()
        self.assertFalse(self.target.exists())
        path.unlink()
        with patch.object(session_start.os, 'open', side_effect=PermissionError):
            self.run_hook()
        self.assertFalse(self.target.exists())

    def test_cli_bootstrap_stays_silent_for_missing_manifest_and_invalid_input(self):
        for path in (self.file, self.root/'missing.toml'):
            result = subprocess.run([sys.executable, '-m', 'coordkit.cli', '--manifest', str(path),
                                     'deliver', '--session-start-hook'], input=b'\xff', capture_output=True,
                                    env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src')),
                                    timeout=3)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b'', b''))

    def test_cli_positive_scope_uses_hook_projection_and_stays_silent(self):
        out, err = io.StringIO(), io.StringIO()
        with patch('sys.stdin', io.StringIO(json.dumps(self.payload))), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(main(['--manifest', str(self.file), 'deliver', '--session-start-hook']), 0)
        self.assertEqual((out.getvalue(), err.getvalue()), ('', ''))
        self.consume(self.target, 's1')

    def test_cli_outer_deadline_also_bounds_bootstrap(self):
        with patch.object(hook_runtime, 'HOOK_TIMEOUT', .02), patch.object(
                deliver, 'bootstrap', side_effect=lambda *args: time.sleep(1)):
            out, err = io.StringIO(), io.StringIO()
            before = time.monotonic()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(main(['--manifest', str(self.file), 'deliver', '--session-start-hook']), 0)
            self.assertLess(time.monotonic() - before, .5)
            self.assertEqual((out.getvalue(), err.getvalue()), ('', ''))


class SessionStartInstallTests(FakeCase):
    def test_owned_synchronous_hook_preserves_plugin_and_uninstalls_only_its_own(self):
        target = self.root/'settings.json'
        original = {'hooks': {'SessionStart': [{'matcher': 'startup', 'hooks': [
            {'type': 'command', 'command': 'plugin-existing', 'async': True}]}]}}
        write_json(target, original)
        deliver.install(self.manifest, target)
        installed = read_json(target)
        hook = installed['hooks']['SessionStart'][-1]['hooks'][0]
        self.assertIn(' deliver --session-start-hook', hook['command'])
        self.assertFalse(hook.get('async', False))
        self.assertEqual(hook['timeout'], deliver.HOOK_TIMEOUT)
        self.assertEqual(installed['hooks']['SessionStart'][0], original['hooks']['SessionStart'][0])
        deliver.install(self.manifest, target)
        self.assertEqual(read_json(target), installed)
        deliver.uninstall(self.manifest, target)
        self.assertEqual(hooks.entries(read_json(target)), hooks.entries(original))

    def test_old_receipts_gain_session_hook_idempotently_and_keep_original_rollback(self):
        for events in (('PostToolUse', 'UserPromptSubmit'), ('PostToolUse', 'UserPromptSubmit', 'Stop')):
            target = self.root/('settings-' + str(len(events)) + '.json')
            original = {'hooks': {'SessionStart': [{'hooks': [{'type': 'command', 'command': 'plugin'}]}]}}
            write_json(target, original)
            with patch.object(deliver, 'HOOK_EVENTS', events):
                deliver.install(self.manifest, target)
            receipt = deliver.receipt_path(self.manifest, target)
            before = copy.deepcopy(read_json(receipt)['before'])
            current = read_json(target)
            current['theme'] = 'unrelated'
            write_json(target, current)
            hooks.reconcile(self.manifest)
            deliver.install(self.manifest, target)
            self.assertEqual(len(read_json(target)['hooks']['SessionStart']), 2)
            self.assertEqual(read_json(receipt)['before'], before)
            deliver.uninstall(self.manifest, target)
            self.assertEqual(hooks.entries(read_json(target)), hooks.entries(original))
            self.assertEqual(read_json(target)['theme'], 'unrelated')

    def test_unowned_session_command_and_owned_drift_are_conflicts(self):
        target = self.root/'settings.json'
        with patch.object(deliver, 'HOOK_EVENTS', ('PostToolUse', 'UserPromptSubmit', 'Stop')):
            deliver.install(self.manifest, target)
        current = read_json(target)
        current['hooks']['SessionStart'] = [{'hooks': [deliver.hook_spec(self.manifest, 'SessionStart')]}]
        write_json(target, current)
        with self.assertRaisesRegex(Exception, 'unowned SessionStart'):
            hooks.reconcile(self.manifest)
        self.assertEqual(read_json(target), current)
        del current['hooks']['SessionStart']
        write_json(target, current)
        hooks.reconcile(self.manifest)
        current = read_json(target)
        current['hooks']['SessionStart'][0]['hooks'][0]['async'] = True
        write_json(target, current)
        with self.assertRaisesRegex(Exception, 'hook_rollback_conflict'):
            deliver.uninstall(self.manifest, target)
        self.assertEqual(read_json(target), current)
