"""Noise limits and the single scheduled runner, using disposable state only."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import threading
from unittest.mock import patch

from coordkit import addressed, bus, lifecycle, questions, reconcile
from coordkit.process import ToolError
from coordkit.state import read_json, write_json
from tests.support import FakeCase


class SimplifyTests(FakeCase):
    def test_one_scheduled_runner_preserves_local_then_remote_order_and_cadence(self):
        self.manifest.launchd.path = [str(Path.home()/'.local/bin'), '/usr/bin', '/bin']
        self.assertEqual(lifecycle.templates(self.manifest)[0]['StartInterval'], 300)
        self.manifest.questions.config_dirs = ['unused-test-config']
        events = []
        state = self.manifest.paths.root/'reconcile/state.json'
        write_json(state, {'last_run': 900})
        self.manifest.launchd.path = [str(Path.home()/'.local/bin'), '/usr/bin', '/bin']
        jobs = lifecycle.templates(self.manifest)
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0]['Label'].endswith('.reconcile'))
        self.assertEqual(jobs[0]['ProgramArguments'][-1], 'sweep')
        self.assertEqual(jobs[0]['StartInterval'], 60)
        with patch.object(addressed, 'sweep', side_effect=lambda *a, **kw: events.append('local') or []), \
             patch.object(questions, 'sweep', side_effect=lambda *a, **kw: events.append('questions')), \
             patch.object(reconcile, 'main', side_effect=lambda *a: events.append('remote') or 6), \
             patch('time.time', return_value=1000):
            self.assertEqual(addressed.main([], self.manifest, 'sweep'), 0)
            self.assertEqual(events, ['local', 'questions'])
            events.clear()
            with patch('time.time', return_value=1301):
                self.assertEqual(addressed.main([], self.manifest, 'sweep'), 6)
            self.assertEqual(events, ['local', 'questions', 'remote'])

    def test_dead_owner_obligations_lead_the_digest_and_wake_the_lead_once(self):
        dead = [dict(id='a1b2c3', owner='worker-x', age=7200, priority='normal', kind='ask', frm='relay', prs=[], flags=[], since=0, due_after=0)]
        sent = []
        with patch.object(bus, 'main', side_effect=lambda argv, manifest: sent.append(argv)):
            state = {}
            addressed.flush_lead_digest(self.manifest, state, 10000, out=lambda _: None, pending=dead, dead=dead)
            addressed.flush_lead_digest(self.manifest, state, 20000, out=lambda _: None, pending=dead, dead=dead)
            addressed.flush_lead_digest(self.manifest, state, 30000, out=lambda _: None, pending=dead, dead=[])
        kinds = [argv[argv.index('--kind') + 1] for argv in sent]
        self.assertEqual(kinds[:2], ['ask', 'fyi'])  # unchanged dead set: no second wake
        self.assertIn('RE-HOME 1', sent[0][-1])
        self.assertIn('a1b2c3->worker-x', sent[0][-1])
        self.assertEqual(state['dead_owner_ids'], [])

    def test_digest_failure_preserves_queue_and_clock(self):
        addressed.to_lead_digest(self.manifest, 'test', 'keep me', at=10000)
        state = {}
        with patch.object(bus, 'main', side_effect=ToolError('bus', 6, 'down')):
            with self.assertRaises(ToolError):
                addressed.flush_lead_digest(self.manifest, state, 10000)
        self.assertIn('keep me', (addressed.home(self.manifest)/'lead-digest.ndjson').read_text())
        self.assertNotIn('lead_digest_at', state)

    def test_digest_drain_keeps_a_concurrent_writer(self):
        addressed.to_lead_digest(self.manifest, 'first', 'first', at=10000)
        started, finished = threading.Event(), threading.Event()
        def writer():
            started.set()
            addressed.to_lead_digest(self.manifest, 'late', 'late', at=10000)
            finished.set()
        workers = []
        def publish(*args):
            t = threading.Thread(target=writer)
            workers.append(t); t.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(finished.is_set())
        try:
            with patch.object(bus, 'main', side_effect=publish):
                addressed.flush_lead_digest(self.manifest, {}, 10000, out=lambda _: None)
        finally:
            for t in workers:
                t.join(3)
        self.assertTrue(finished.is_set())
        queued = (addressed.home(self.manifest)/'lead-digest.ndjson').read_text()
        self.assertIn('late', queued)
        self.assertNotIn('first', queued)
        self.assertFalse(list(addressed.home(self.manifest).glob('lead-digest.sent-*')))

    def test_reassignment_gets_new_owner_one_wake_and_reply_clears_digest(self):
        self.manifest.addressed.agents = {'worker': {}, 'other': {}}
        self.manifest.addressed.roles = {}
        self.manifest.bus.mirror = False
        bus.configure(self.manifest)
        bus.append([dict(id='q1', ts='1970-01-01T00:00:01Z', frm='sender', to='worker',
                         kind='ask', topic='general', text='ship?')], project=False)
        quiet = lambda _: None
        with patch.object(addressed, 'listening', return_value=(True, 'test')):
            addressed.sweep(self.manifest, at=10000, out=quiet)
            addressed.sweep(self.manifest, at=20000, out=quiet)
            with contextlib.redirect_stdout(io.StringIO()):
                bus.main(['ack','q1','--from','worker','--owner','other'], self.manifest)
            # Read the actual assignment clock written by bus ack.
            since = addressed.parse_ts(bus.read()[-1]['ts']).timestamp()
            addressed.sweep(self.manifest, at=since+1000, out=quiet)
            wakes = [r for r in bus.read() if r['id'].startswith('wake-ob-')]
            self.assertEqual([r['to'] for r in wakes], ['worker','other'])
            with contextlib.redirect_stdout(io.StringIO()):
                bus.main(['ack','q1','--from','other'], self.manifest)
            with patch.object(addressed, 'flush_lead_digest') as flush:
                addressed.sweep(self.manifest, at=since+2000, out=quiet)
            self.assertEqual(flush.call_args.kwargs['pending'], [])

    def test_shared_transport_alias_ack_closes_old_coordinator_obligation(self):
        log = self.manifest.paths.bus_log
        path = log.parent/'state'/'aliases.json'
        write_json(path, {'coordinator':'relay'})
        rows = [dict(id='q', ts='1970-01-01T00:00:01Z', frm='worker', to='coordinator', kind='ask', text='ship?'),
                dict(id='a', ts='1970-01-01T00:00:02Z', frm='relay', to='worker', kind='ack', re='q', text='received')]
        from coordkit import obligations
        self.assertEqual(obligations.open_obligations(self.manifest, rows, 10000), [])

    def test_compat_wait_and_ack_routes_keep_the_announced_arguments(self):
        source = (Path(__file__).resolve().parents[1]/'examples/omi/bin/coordkit').read_text()
        fake = self.root/'bus'
        fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n'); fake.chmod(0o755)
        wrapper = self.root/'coordkit'
        # The wrapper exposes its transport through an environment override.
        wrapper.write_text(source)
        for args, want in [(['wait','--as','lane','--timeout','0'], ['wait','--to','lane','--timeout','0']),
                           (['ack','abc','--as','lane','done','merged'], ['ack','abc','--from','lane','done: merged'])]:
            result = subprocess.run(['/bin/sh',str(wrapper),*args], capture_output=True, text=True, timeout=5,
                                    env=dict(os.environ, COORDKIT_BUS=str(fake)))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), want)

    def test_lead_hook_verification_probes_an_unrelated_session(self):
        source = (Path(__file__).resolve().parents[1]/'examples/omi/bin/apply-lead-hooks.sh').read_text()
        block = source[source.index('probe() {'):source.index('ck wait pending --brief >')]
        prelude = r'''SID=11111111-1111-4111-8111-111111111111
rollback() { echo "$*" >&2; exit 1; }
ck() {
  payload=$(cat)
  case "$payload" in
    *"$SID"*) if [ "$running" = 1 ]; then echo '{"permissionDecision": "deny"}'; fi ;;
    *) printf '%s\n' other >> "$PROBE_LOG" ;;
  esac
}
'''
        for running in (0, 1):
            log = self.root/f'probes-{running}'
            env = dict(os.environ, PROBE_LOG=str(log), LOG=str(self.root/'probe-errors.log'))
            result = subprocess.run(['bash','-u','-c',f'running={running}\n'+prelude+block],
                                    capture_output=True, text=True, env=env, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(log.read_text().splitlines(), ['other'])
