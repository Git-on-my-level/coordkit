"""Resume holder probes distinguish live holders from stale or reused PID records."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from unittest.mock import patch

from coordkit import deliver, identity
from coordkit.adapters import claude_resume as resume
from coordkit.manifest import load
from coordkit.process import ToolError
from coordkit.usage import transcript_dir
from coordkit.state import write_json
from tests.support import FakeCase


class ClaudeHolderTests(FakeCase):
    def setUp(self):
        super().setUp()
        self.session = uuid.uuid4().hex
        self.file.write_text(self.file.read_text() + f'''
[[lanes]]
name="worker"
config_dir="identity"
cwd="."
model="test"
effort="test"
session_id="{self.session}"
resume="headless"
''')
        self.manifest = load(self.file)
        self.lane = self.manifest.lanes[0]

    def result(self, out='', code=0, err=''):
        return subprocess.CompletedProcess([], code, out, err)

    def test_process_snapshot_ignores_zombies_and_preserves_full_argv(self):
        text = ('123 1 S Fri Nov  6 12:00:00 2026 claude --resume '+self.session+' -p task\n'
                '124 1 Z Fri Nov  6 12:00:00 2026 zombie '+self.session+'\ninvalid\n')
        with patch.object(resume, 'probe', return_value=self.result(text)):
            rows = resume.processes()
        self.assertEqual(set(rows), {123})
        self.assertEqual(rows[123]['ppid'], 1)
        self.assertEqual(rows[123]['started'], 'Fri Nov 6 12:00:00 2026')
        self.assertIn('--resume '+self.session, rows[123]['argv'])

    def test_resume_arg_and_session_elsewhere_in_argv_refuse_without_lsof(self):
        with patch.object(resume, 'lsof_binary', side_effect=ToolError('lsof', 6, 'lsof_unavailable')):
            for argv in ('claude --resume '+self.session, 'remote-control --session='+self.session,
                         'helper /transcripts/'+self.session+'.jsonl'):
                self.assertTrue(resume.holder_present(self.manifest, self.lane, {12: {'argv': argv}}))
            with self.assertRaisesRegex(ToolError, 'lsof_unavailable'):
                resume.holder_present(self.manifest, self.lane, {12: {'argv': 'unrelated'}})

    def test_pid_identity_start_time_match_mismatch_and_comm_fallback(self):
        snapshot = {12: {'started': 'new start', 'argv': 'unrelated'}}
        with patch.object(resume, 'probe', return_value=self.result('/bin/claude\n')) as probe:
            self.assertTrue(resume.pid_matches(12, {'pid': 12, 'started': 'new start'}, snapshot))
            self.assertFalse(resume.pid_matches(12, {'pid': 12, 'started': 'old start'}, snapshot))
            probe.assert_not_called()
            self.assertTrue(resume.pid_matches(12, {}, snapshot))
        with patch.object(resume, 'probe', return_value=self.result('/bin/unrelated\n')):
            self.assertFalse(resume.pid_matches(12, {}, snapshot))
        self.assertFalse(resume.pid_matches(99, {}, snapshot))

    def test_open_transcript_holder_and_missing_tool_refusal(self):
        path = transcript_dir(self.lane)/f'{self.session}.jsonl'
        path.parent.mkdir(parents=True)
        path.touch()
        with patch.object(resume.shutil, 'which', return_value='/test/lsof'), \
                patch.object(resume, 'probe', return_value=self.result('321\n')) as probe:
            self.assertTrue(resume.holder_present(self.manifest, self.lane, {}))
            self.assertEqual(probe.call_args.args[0], ['/test/lsof', '-nP', '-t', '--', str(path)])
        with patch.object(resume, 'lsof_binary', side_effect=ToolError('lsof', 6, 'lsof_unavailable')), \
                patch.object(resume, 'probe') as probe:
            with self.assertRaisesRegex(ToolError, 'lsof_unavailable'):
                resume.holder_present(self.manifest, self.lane, {})
            probe.assert_not_called()

    def test_holder_probe_error_is_not_mistaken_for_no_holder(self):
        with patch.object(resume, 'probe', return_value=self.result(code=2)):
            with self.assertRaises(ToolError):
                resume.processes()
        path = transcript_dir(self.lane)/f'{self.session}.jsonl'
        path.parent.mkdir(parents=True)
        path.touch()
        with patch.object(resume.shutil, 'which', return_value='/test/lsof'), \
                patch.object(resume, 'probe', return_value=self.result(code=1, err='probe failed')):
            with self.assertRaises(ToolError):
                resume.holder_present(self.manifest, self.lane, {})

    def test_live_process_with_session_argv_is_a_holder(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', '--resume', self.session],
                                 start_new_session=True, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            with patch.object(resume, 'PROBE_TIMEOUT', 20):
                self.assertTrue(resume.holder_present(self.manifest, self.lane, resume.processes()))
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=3)

    def test_lsof_finds_live_open_file_without_session_in_argv(self):
        try:
            resume.lsof_binary()
        except ToolError:
            self.skipTest('real lsof unavailable; refusal covered separately')
        path = transcript_dir(self.lane)/f'{self.session}.jsonl'
        path.parent.mkdir(parents=True)
        path.touch()
        ready = self.root/'ready'
        code = ('import os,time; from pathlib import Path; '
                'stream=open(os.environ["CK_OPEN_PATH"]); '
                'Path(os.environ["CK_OPEN_READY"]).touch(); time.sleep(60)')
        child = subprocess.Popen([sys.executable, '-c', code], start_new_session=True,
                                 env=dict(os.environ, CK_OPEN_PATH=str(path), CK_OPEN_READY=str(ready)),
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic()+3
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(ready.exists())
            # A full-host lsof sweep can exceed the production fail-closed budget
            # on a loaded test host; allow headroom only for this integration check.
            with patch.object(resume, 'PROBE_TIMEOUT', 20):
                snapshot = resume.processes()
                self.assertNotIn(self.session, snapshot[child.pid]['argv'])
                self.assertTrue(resume.holder_present(self.manifest, self.lane, snapshot))
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=3)

    def test_probes_use_bounded_timeout_and_fixed_locale(self):
        with patch.object(resume.subprocess, 'run', return_value=self.result()) as run:
            resume.probe(['ps'])
        self.assertEqual(run.call_args.kwargs['timeout'], 2)
        self.assertEqual(run.call_args.kwargs['env']['LC_ALL'], 'C')

    def test_explicit_transcript_bindings_and_matching_hook_note_are_probed(self):
        bound = self.root/'bound.jsonl'
        hooked = self.root/'hooked.jsonl'
        write_json(identity.home(self.manifest)/'sessions.json', {
            self.session: {'name': 'worker', 'transcript_path': str(bound)}})
        note = deliver.note_path(self.manifest, 'worker')
        write_json(note, {'session_id': self.session, 'transcript_path': str(hooked)})
        paths = resume.transcript_paths(self.manifest, self.lane)
        self.assertIn(bound, paths)
        self.assertIn(hooked, paths)
        write_json(note, {'session_id': 'other-session', 'transcript_path': str(hooked)})
        self.assertNotIn(hooked, resume.transcript_paths(self.manifest, self.lane))

    def test_lsof_discovery_path_then_standard_locations(self):
        with patch.object(resume.shutil, 'which', return_value='/path/lsof'), \
                patch.object(resume.os.path, 'isfile') as isfile:
            self.assertEqual(resume.lsof_binary(), '/path/lsof')
            isfile.assert_not_called()
        for expected in ('/usr/sbin/lsof', '/usr/bin/lsof'):
            with patch.object(resume.shutil, 'which', return_value=None), \
                    patch.object(resume.os.path, 'isfile', side_effect=lambda p: p == expected), \
                    patch.object(resume.os, 'access', return_value=True):
                self.assertEqual(resume.lsof_binary(), expected)
        with patch.object(resume.shutil, 'which', return_value=None), \
                patch.object(resume.os.path, 'isfile', return_value=False):
            with self.assertRaisesRegex(ToolError, 'lsof_unavailable'):
                resume.lsof_binary()
        with patch.object(resume.shutil, 'which', return_value=None), \
                patch.object(resume.os.path, 'isfile', return_value=True), \
                patch.object(resume.os, 'access', return_value=False):
            with self.assertRaisesRegex(ToolError, 'lsof_unavailable'):
                resume.lsof_binary()

    def test_lsof_errors_and_malformed_output_refuse_detection(self):
        path = transcript_dir(self.lane)/f'{self.session}.jsonl'
        path.parent.mkdir(parents=True)
        path.touch()
        results = [self.result(code=2), self.result('bad-output'), self.result(err='warning')]
        with patch.object(resume, 'lsof_binary', return_value='/test/lsof'):
            for result in results:
                with patch.object(resume, 'probe', return_value=result):
                    with self.assertRaisesRegex(ToolError, 'lsof_probe_failed'):
                        resume.holder_present(self.manifest, self.lane, {})
            for error in (OSError('exec failed'), subprocess.TimeoutExpired('lsof', 2)):
                with patch.object(resume, 'probe', side_effect=error):
                    with self.assertRaisesRegex(ToolError, 'lsof_probe_failed'):
                        resume.holder_present(self.manifest, self.lane, {})

    def test_session_lock_key_is_resolved_config_and_session_only(self):
        config = Path(self.lane.config_dir)
        config.mkdir()
        alias = self.root/'identity-alias'
        alias.symlink_to(config, target_is_directory=True)
        original = resume.lock_path(self.lane)
        self.assertEqual(original, resume.lock_path(replace(self.lane, name='other', cwd='/elsewhere',
                                                            config_dir=str(alias))))
        self.assertNotEqual(original, resume.lock_path(replace(self.lane, session_id='other-session')))
        self.assertNotEqual(original, resume.lock_path(replace(self.lane, config_dir=str(self.root/'other-config'))))
        self.assertEqual(original.parent, self.root/'xdg-state/coordkit/resume-locks')
        with patch.dict(os.environ, XDG_STATE_HOME=''), patch.object(Path, 'home', return_value=self.root):
            self.assertEqual(resume.lock_path(self.lane).parent, self.root/'.local/state/coordkit/resume-locks')

    def test_session_lock_is_private_and_excludes_another_process(self):
        path = resume.lock_path(self.lane)
        code = ('import sys; from types import SimpleNamespace; '
                'from coordkit.adapters.claude_resume import session_lock; '
                'lane=SimpleNamespace(config_dir=sys.argv[1],session_id=sys.argv[2]); '
                '\nwith session_lock(lane) as held: print("held" if held else "busy")')
        previous = os.umask(0)
        try:
            with resume.session_lock(self.lane):
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                result = subprocess.run([sys.executable, '-c', code, self.lane.config_dir, self.session],
                                        env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src')),
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual((result.returncode, result.stdout.strip()), (0, 'busy'), result.stderr)
        finally:
            os.umask(previous)
        with resume.session_lock(self.lane) as held:
            self.assertIsNotNone(held)
