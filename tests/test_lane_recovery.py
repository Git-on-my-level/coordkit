"""Recovery of existing lane plans survives removal of the public lane launcher."""
import contextlib
import io
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time
from unittest.mock import patch

from coordkit import lane, reconcile
from coordkit.state import read_json, write_json
from tests.support import FakeCase


class LaneRecoveryTests(FakeCase):
    def plan(self):
        path = self.manifest.paths.out_dir/'req/worker.plan.json'
        plan = dict(label='worker', key='ck-sample-worker', started=time.time(), plan_file=str(path),
                    card='card:task', execution_id='exec-existing', phase='running', prompt='Complete safely.',
                    pool='implement', owns=[], access='read_only', bus_start=0, autocontinued=True)
        write_json(path, plan)
        self.manifest.paths.bus_log.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.paths.bus_log.write_text('')
        return plan

    def quiet(self, fn, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return fn(*args, **kwargs)

    def stop(self, child):
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=5)

    def test_detached_internal_cli_watcher_records_terminal(self):
        plan = self.plan()
        child = self.quiet(lane.detach_watcher, self.manifest, plan)
        try:
            self.assertEqual(child.wait(timeout=15), 0)
            saved = read_json(Path(plan['plan_file']))
            self.assertEqual((saved['phase'], saved['terminal']), ('terminal', 'completed'))
            self.assertEqual(saved['watcher_pid'], child.pid)
            self.assertTrue(any('release' in row['args'] for row in self.calls()))
            self.assertFalse(any('delegate' in row['args'] for row in self.calls()))
        finally:
            self.stop(child)

    def test_reconcile_restores_a_killed_watcher_after_the_lease_ttl(self):
        plan = self.plan()
        lock = Path(plan['plan_file']).with_suffix('.watch.lock')
        code = ("import fcntl,signal,sys; f=open(sys.argv[1],'a'); "
                "fcntl.flock(f,fcntl.LOCK_EX); print('ready',flush=True); signal.pause()")
        child = subprocess.Popen([sys.executable, '-c', code, str(lock)],
                                 stdout=subprocess.PIPE, text=True, start_new_session=True)
        try:
            self.assertTrue(select.select([child.stdout], [], [], 5)[0], 'watcher lock startup timed out')
            self.assertEqual(child.stdout.readline().strip(), 'ready')
            with patch.object(lane, 'detach_watcher') as restored:
                self.assertFalse(self.quiet(lane.recover, self.manifest, plan))
                restored.assert_not_called()
            self.stop(child)
            self.respond({'tool': 'agentctl', 'contains': ['status'],
                          'json': {'ok': True, 'result': {'state': 'running'}}})
            self.manifest.lane.max_run_hours = 4
            with patch.object(lane.time, 'time', return_value=plan['started'] + 7201), \
                    patch.object(lane, 'detach_watcher') as restored:
                self.assertIn(self.quiet(reconcile.reconcile, self.manifest, at=plan['started'] + 7201), (0, 5))
                restored.assert_called_once()
            self.assertTrue(any('renew' in row['args'] for row in self.calls()))
            self.respond()
            self.assertEqual(self.quiet(lane.main, ['_watch', plan['plan_file']], self.manifest), 0)
            self.assertEqual(read_json(Path(plan['plan_file']))['terminal'], 'completed')
            self.assertFalse(any('delegate' in row['args'] for row in self.calls()))
        finally:
            self.stop(child)
            child.stdout.close()
