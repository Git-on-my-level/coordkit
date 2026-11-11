"""Opt-in Claude resume uses the same session and leaves long turns detached."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid
from unittest.mock import Mock, patch

from coordkit import addressed, wake
from coordkit.adapters.builtin import ClaudeCode
from coordkit.adapters import claude_resume
from coordkit.manifest import ManifestError, load
from coordkit.process import ToolError
from coordkit.state import read_json, write_json
from coordkit.wake import wake_adapters as discover_wake_adapters
from tests.support import FakeCase


LANE = '''
[[lanes]]
name = "worker"
config_dir = "identity with spaces"
cwd = "project with spaces"
model = "configured"
effort = "configured"
session_id = "session-pinned"
resume = "headless"
'''


class ClaudeResumeTests(FakeCase):
    def setUp(self):
        super().setUp()
        self.file.write_text(self.file.read_text() + LANE.replace('session-pinned', uuid.uuid4().hex))
        self.manifest = load(self.file)
        self.lane = self.manifest.lanes[0]
        Path(self.lane.cwd).mkdir()
        self.capture = self.root/'captured.json'
        self.done = self.root/'done'
        binary = self.root/'fakes'/'claude'
        binary.write_text('#!' + sys.executable + ' -S\n' + '''import json, os, sys, time
from pathlib import Path
Path(os.environ['CK_RESUME_CAPTURE']).write_text(json.dumps(dict(
    argv=sys.argv[1:], stdin=sys.stdin.read(), cwd=os.getcwd(),
    config=os.environ.get('CLAUDE_CONFIG_DIR'), pid=os.getpid(), sid=os.getsid(0))))
if os.environ.get('CK_RESUME_REFUSE'):
    print('No conversation found', flush=True)
    sys.exit(8)
time.sleep(float(os.environ.get('CK_RESUME_HOLD', '0')))
release = os.environ.get('CK_RESUME_RELEASE')
deadline = time.monotonic()+30
while release and not Path(release).exists() and time.monotonic() < deadline:
    time.sleep(.02)
Path(os.environ['CK_RESUME_DONE']).write_text('finished')
''')
        binary.chmod(0o755)
        env = patch.dict(os.environ, PATH=str(binary.parent)+os.pathsep+os.environ['PATH'],
                         CK_RESUME_CAPTURE=str(self.capture), CK_RESUME_DONE=str(self.done),
                         CLAUDE_CONFIG_DIR='inherited-config')
        env.start(); self.addCleanup(env.stop)
        self.children = []
        self.resume_children = []
        actual_popen = subprocess.Popen
        def popen(*args, **kwargs):
            proc = actual_popen(*args, **kwargs)
            if str(args[0][0]) == '/bin/sh':
                self.resume_children.append(proc)
            if str(args[0][0]) != 'ps' and not str(args[0][0]).endswith('/lsof'):
                self.children.append(proc)
            return proc
        spy = patch('coordkit.adapters.builtin.subprocess.Popen', side_effect=popen)
        spy.start(); self.addCleanup(spy.stop)
        self.addCleanup(self.stop_children)
        for target, value in (('coordkit.adapters.builtin.RESUME_REFUSAL_SECONDS', .1),
                              ('coordkit.addressed.PROBE', lambda: set())):
            p = patch(target, value); p.start(); self.addCleanup(p.stop)
        # Adapter tests control process observations; real ps/lsof integration
        # lives in test_claude_holders so host load cannot mask branch coverage.
        def snapshot():
            rows = {os.getpid(): {'started': f'test-start-{os.getpid()}', 'argv': 'test-runner'}}
            for proc in self.resume_children:
                if proc.poll() is None:
                    rows[proc.pid] = {'started': f'test-start-{proc.pid}',
                                      'argv': f'claude --resume {self.lane.session_id}'}
            return rows
        p = patch.object(claude_resume, 'processes', side_effect=snapshot)
        p.start(); self.addCleanup(p.stop)
        p = patch.object(claude_resume, 'start_time', side_effect=lambda pid: f'test-start-{pid}')
        p.start(); self.addCleanup(p.stop)
        p = patch.object(claude_resume, 'lsof_binary', return_value='/test/lsof')
        p.start(); self.addCleanup(p.stop)
        self.adapter = ClaudeCode()

    def stop_children(self):
        for proc in self.children:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass  # it exited between the poll and the signal; macOS answers EPERM for a zombie's group
            proc.wait(timeout=3)

    def wait_for(self, path, timeout=3):
        deadline = time.monotonic()+timeout
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(path.exists(), str(path))

    def resume(self, name='worker'):
        return self.adapter.wake(self.manifest, name, [], 'request-key')

    def test_args_stdin_identity_and_detached_turn_survives_adapter_return(self):
        with patch.dict(os.environ, CK_RESUME_HOLD='.8'):
            result = self.resume()
        self.assertEqual(result['status'], 'woken')
        self.wait_for(self.capture)
        captured = read_json(self.capture)
        self.assertEqual(captured['argv'][:3], ['--resume', self.lane.session_id, '-p'])
        self.assertEqual(len(captured['argv']), 4)
        self.assertNotIn('--fork-session', captured['argv'])
        self.assertIn('whole bus inbox', captured['argv'][3])
        self.assertIn('background bus wait --to worker armed', captured['argv'][3])
        self.assertEqual(captured['stdin'], '')
        self.assertEqual(captured['cwd'], self.lane.cwd)
        self.assertEqual(captured['config'], self.lane.config_dir)
        self.assertEqual(captured['sid'], result['pid'])
        self.assertIsNone(self.children[0].poll())
        pidfile = wake.wake_path(self.manifest, 'worker').with_suffix('.resume.pid')
        self.assertEqual(int(pidfile.read_text()), result['pid'])
        self.assertEqual(self.resume(), {'status': 'unresumable', 'reason': 'holder_present'})
        self.assertEqual(len(self.children), 1)
        self.wait_for(self.done)
        self.children[0].wait(timeout=3)

    def test_long_turn_survives_the_parent_wake_process_exiting(self):
        code = '''import sys
from coordkit.adapters import builtin, claude_resume
from coordkit.manifest import load
claude_resume.processes = lambda: {}
claude_resume.holder_present = lambda *args: False
claude_resume.start_time = lambda pid: 'test-start'
builtin.RESUME_REFUSAL_SECONDS = .1
builtin.ClaudeCode().wake(load(sys.argv[1]), 'worker', [], 'parent-request')
'''
        release = self.root/'release-turn'
        env = dict(os.environ, CK_RESUME_RELEASE=str(release), PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src'))
        parent = subprocess.run([sys.executable, '-c', code, str(self.file)], env=env,
                                capture_output=True, text=True, timeout=8)
        self.assertEqual(parent.returncode, 0, parent.stderr)
        self.wait_for(self.capture)
        pid = read_json(self.capture)['pid']
        try:
            os.kill(pid, 0)
        finally:
            # Release after the parent-exit assertion, independent of scheduler delays.
            release.touch()
            self.wait_for(self.done, timeout=8)

    def test_immediate_failure_is_logged_and_pidfile_cleared(self):
        with patch.dict(os.environ, CK_RESUME_REFUSE='1'), patch(
                'coordkit.adapters.builtin.RESUME_REFUSAL_SECONDS', 2):
            with self.assertRaisesRegex(ToolError, 'No conversation found'):
                self.resume()
        self.assertFalse(wake.wake_path(self.manifest, 'worker').with_suffix('.resume.pid').exists())
        log = wake.wake_path(self.manifest, 'worker').with_suffix('.resume.log')
        self.assertIn('No conversation found', log.read_text())
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_only_explicit_headless_pinned_lanes_can_resume(self):
        for attr, value in (('resume', ''), ('session_id', '')):
            old = getattr(self.lane, attr)
            setattr(self.lane, attr, value)
            self.assertEqual(self.resume()['status'], 'unresumable')
            setattr(self.lane, attr, old)
        self.assertEqual(self.resume('unknown')['status'], 'unresumable')
        self.assertFalse(self.children)
        self.file.write_text(self.file.read_text().replace('resume = "headless"', ''))
        self.assertEqual(load(self.file).lanes[0].resume, '')
        for value in ('"interactive"', 'true', '1'):
            self.file.write_text(self.file.read_text().split('resume =')[0] + 'resume = '+value+'\n')
            with self.assertRaisesRegex(ManifestError, 'lanes.resume'):
                load(self.file)

    def test_listening_rechecked_inside_adapter_and_wait_lock_blocks_resume(self):
        with patch.object(addressed, 'listening', return_value=(True, 'busy: transcript grew')) as listening:
            self.assertEqual(self.resume(), {'status': 'listening', 'evidence': 'busy: transcript grew'})
            listening.assert_called_once()
        lock = wake.wake_path(self.manifest, 'worker').parent.parent/'cursors/worker.wait.lock'
        with addressed.locked(lock):
            self.assertEqual(self.resume()['status'], 'listening')
        self.assertFalse(self.children)

    def test_pidfile_and_resume_lock_prevent_duplicates_and_stale_pid_allows_retry(self):
        path = wake.wake_path(self.manifest, 'worker').with_suffix('.resume.pid')
        with claude_resume.session_lock(self.lane):
            self.assertEqual(self.resume()['status'], 'listening')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(os.getpid()))
        write_json(path.with_suffix('.identity.json'), {'pid': os.getpid(), 'started': claude_resume.start_time(os.getpid())})
        self.assertEqual(self.resume()['status'], 'listening')
        self.assertFalse(self.children)
        # A real exited process supplies a stale positive PID, with no guessing.
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        child.wait(timeout=3)
        write_json(path, {'pid': child.pid})
        self.assertEqual(self.resume()['status'], 'woken')
        self.assertEqual(len(self.children), 2)

    def test_pidfile_failure_stops_unrecorded_child(self):
        with patch.dict(os.environ, CK_RESUME_HOLD='5'), patch(
                'coordkit.state.write_json', side_effect=OSError('read only')):
            with self.assertRaises(OSError):
                self.resume()
        self.assertIsNotNone(self.children[0].poll())

    def test_claude_precedes_agentctl_and_unresumable_falls_through(self):
        fallback = Mock(name='agentctl', spec=['name', 'wake'])
        fallback.name = 'agentctl'
        fallback.wake.return_value = {'status': 'woken'}
        with patch('coordkit.adapters.builtin.Agentctl', return_value=fallback):
            adapters = discover_wake_adapters(self.manifest)
        self.assertEqual([a.name for a in adapters], ['claude-code', 'hermes', 'agentctl'])
        row = dict(id='ask', frm='lead', to='worker', kind='ask')
        with patch.object(wake, 'wake_adapters', return_value=adapters), \
                patch.object(wake, 'is_listening', return_value=False):
            self.lane.resume = ''
            wake.notify(self.manifest, [row])
        fallback.wake.assert_called_once()
        record = read_json(wake.wake_path(self.manifest, 'worker'))
        self.assertEqual([a['status'] for a in record['attempts']], ['unresumable', 'woken'])

    def test_race_listening_stops_adapter_chain_and_success_does_too(self):
        fallback = Mock(name='fallback', spec=['name', 'wake'])
        fallback.name = 'fallback'
        path = wake.wake_path(self.manifest, 'worker')
        row = dict(id='ask', frm='lead', to='worker', kind='ask')
        with patch.object(wake, 'wake_adapters', return_value=[self.adapter, fallback]), \
                patch.object(wake, 'is_listening', return_value=False):
            with patch.object(addressed, 'listening', return_value=(True, 'busy')):
                wake.notify(self.manifest, [row])
            self.assertEqual(read_json(path)['status'], 'listening')
            path.unlink()
            wake.notify(self.manifest, [row])
            wake.notify(self.manifest, [dict(row, id='second')])
        fallback.wake.assert_not_called()
        self.assertEqual(read_json(path)['status'], 'woken')
        self.assertEqual(len(self.children), 1)

    def test_holder_guard_returns_unresumable_before_any_launch(self):
        with patch.object(claude_resume, 'holder_present', return_value=True):
            self.assertEqual(self.resume(), {'status': 'unresumable', 'reason': 'holder_present'})
        self.assertFalse(self.children)

    def test_live_pid_with_mismatched_start_time_is_stale(self):
        path = wake.wake_path(self.manifest, 'worker').with_suffix('.resume.pid')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(os.getpid()))
        write_json(path.with_suffix('.identity.json'), {'pid': os.getpid(), 'started': 'another process start'})
        self.assertEqual(self.resume()['status'], 'woken')
        self.assertEqual(len(self.children), 1)

    def test_child_records_pid_when_parent_dies_immediately_after_popen(self):
        code = '''import os, subprocess, sys
from coordkit.adapters import builtin, claude_resume
from coordkit.manifest import load
claude_resume.processes = lambda: {}
claude_resume.holder_present = lambda *args: False
claude_resume.start_time = lambda pid: 'test-start'
real_popen = subprocess.Popen
def start(argv, **kwargs):
    proc = real_popen(argv, **kwargs)
    if argv[0] == '/bin/sh':
        os._exit(0)
    return proc
subprocess.Popen = start
builtin.ClaudeCode().wake(load(sys.argv[1]), 'worker', [], 'crash-request')
'''
        env = dict(os.environ, CK_RESUME_HOLD='5', PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src'))
        try:
            parent = subprocess.run([sys.executable, '-c', code, str(self.file)], env=env,
                                    capture_output=True, text=True, timeout=8)
            self.assertEqual(parent.returncode, 0, parent.stderr)
            self.wait_for(self.capture)
            pid = read_json(self.capture)['pid']
            path = wake.wake_path(self.manifest, 'worker').with_suffix('.resume.pid')
            self.assertEqual(int(path.read_text()), pid)
            self.assertFalse(path.with_suffix('.identity.json').exists())
            snapshot = {pid: {'started': 'surviving-child', 'argv': 'claude'}}
            with patch.object(claude_resume, 'processes', return_value=snapshot), patch.object(
                    claude_resume, 'probe', return_value=subprocess.CompletedProcess([], 0, '/test/claude', '')):
                self.assertEqual(self.resume()['status'], 'listening')
            self.assertEqual(len(self.children), 1)  # only the exited parent, no second resume
        finally:
            self.wait_for(self.done, timeout=8)

    def test_pid_write_failure_never_executes_claude(self):
        original = subprocess.Popen
        def start(argv, **kwargs):
            if argv[0] == '/bin/sh':
                argv = list(argv)
                argv[5] = str(self.root)  # redirecting echo onto a directory fails
            return original(argv, **kwargs)
        with patch('coordkit.adapters.builtin.subprocess.Popen', side_effect=start), \
                patch('coordkit.adapters.builtin.RESUME_REFUSAL_SECONDS', 2), self.assertRaises(ToolError):
            self.resume()
        self.assertFalse(self.capture.exists())
        self.assertIsNotNone(self.children[0].poll())

    def test_process_probe_failure_refuses_resume(self):
        with patch.object(claude_resume, 'processes', side_effect=OSError('ps unavailable')):
            self.assertEqual(self.resume(), {'status': 'unresumable', 'reason': 'holder_probe_failed'})
        self.assertFalse(self.children)

    def test_holder_reason_applies_even_with_a_matching_pid_record(self):
        path = wake.wake_path(self.manifest, 'worker').with_suffix('.resume.pid')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(os.getpid()))
        write_json(path.with_suffix('.identity.json'), {'pid': os.getpid(), 'started': f'test-start-{os.getpid()}'})
        with patch.object(claude_resume, 'holder_present', return_value=True):
            self.assertEqual(self.resume(), {'status': 'unresumable', 'reason': 'holder_present'})
        self.assertFalse(self.children)

    def test_lsof_refusal_reason_is_durable_in_wake_record(self):
        row = dict(id='ask', frm='lead', to='worker', kind='ask')
        for reason in ('lsof_unavailable', 'lsof_probe_failed'):
            path = wake.wake_path(self.manifest, 'worker')
            path.unlink(missing_ok=True)
            with patch.object(wake, 'wake_adapters', return_value=[self.adapter]), \
                    patch.object(wake, 'is_listening', return_value=False), \
                    patch.object(claude_resume, 'lsof_binary', side_effect=ToolError('lsof', 6, reason)):
                wake.notify(self.manifest, [row])
            record = read_json(path)
            self.assertEqual(record['status'], 'unresumable')
            self.assertEqual(record['attempts'][-1]['reason'], reason)
        self.assertFalse(self.children)

    def test_holder_is_rechecked_under_session_lock_immediately_before_launch(self):
        with patch.object(claude_resume, 'holder_present', side_effect=[False, True]) as holder:
            self.assertEqual(self.resume(), {'status': 'unresumable', 'reason': 'holder_present'})
        self.assertEqual(holder.call_count, 2)
        self.assertFalse(self.children)
        self.assertFalse(wake.wake_path(self.manifest, 'worker').with_suffix('.resume.pid').exists())

    def test_two_manifest_roots_for_same_session_launch_exactly_once(self):
        other_file = self.root/'other.toml'
        other_file.write_text(self.file.read_text()+'\n[paths]\nroot="other-state"\n')
        other = load(other_file)
        self.assertNotEqual(self.manifest.paths.root, other.paths.root)
        self.assertEqual(claude_resume.lock_path(self.lane), claude_resume.lock_path(other.lanes[0]))
        entered, release = threading.Event(), threading.Event()
        results, errors = [], []
        def holder(manifest, lane, snapshot):
            if manifest.file == self.file and not entered.is_set():
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('test release not signalled')
            return False
        def first():
            try:
                results.append(self.resume())
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=first)
        with patch.object(claude_resume, 'holder_present', side_effect=holder), \
                patch.dict(os.environ, CK_RESUME_HOLD='3'):
            try:
                thread.start()
                self.assertTrue(entered.wait(5))
                results.append(self.adapter.wake(other, 'worker', [], 'other-key'))
            finally:
                release.set()
                thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sorted(r['status'] for r in results), ['listening', 'woken'])
        self.assertEqual(len(self.resume_children), 1)
