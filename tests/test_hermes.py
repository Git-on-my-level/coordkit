"""Hermes lanes run one turn at a time: wake, end-of-turn re-check, receipts and reachability."""
import datetime as dt
import io
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

from coordkit import addressed, bus, deliver, usage, wake
from coordkit.adapters import available, hermes, hermes_turn
from coordkit.adapters.hermes import Hermes
from coordkit.manifest import ManifestError, load
from coordkit.process import ToolError
from coordkit.state import read_json, write_json
from tests.support import BASE, FakeCase

LANE = '''
[addressed.agents.ops]
runtime = "hermes"
[[lanes]]
name = "ops"
config_dir = "profile home"
cwd = "project dir"
model = "vendor-model"
effort = "default"
harness = "hermes"
profile = "ops"
'''

FAKE = '''import json, os, sys, time
from pathlib import Path
argv = sys.argv[1:]
log = Path(os.environ['CK_HERMES_LOG'])
if 'sessions' in argv:
    with open(os.environ['CK_HERMES_LOG'] + '.admin', 'a') as stream:
        stream.write(json.dumps(argv) + '\\n')
    sys.exit(0)
calls = len(log.read_text().splitlines()) if log.exists() else 0
with log.open('a') as stream:
    stream.write(json.dumps(dict(argv=argv, cwd=os.getcwd(), as_=os.environ.get('COORDKIT_AS'))) + '\\n')
resumed = argv[argv.index('--resume') + 1] if '--resume' in argv else ''
if resumed and resumed == os.environ.get('CK_HERMES_GONE'):
    print(json.dumps(dict(type='result', status='failed', error='session not found: ' + resumed)))
    sys.exit(1)
if resumed and resumed == os.environ.get('CK_HERMES_CRASH'):
    sys.exit(1)  # died after possibly doing work: no terminal record
usage = argv[argv.index('--usage-file') + 1]
fail = os.environ.get('CK_HERMES_FAIL')
Path(usage).write_text(json.dumps(dict(input_tokens=100, output_tokens=7, cache_read_tokens=30,
    cache_write_tokens=5, reasoning_tokens=2, api_calls=3, model='vendor-model', provider='vendor',
    session_id='sess-1', failed=bool(fail))))
late = os.environ.get('CK_HERMES_LATE_ROW')
if late and calls == 0:
    with open(os.environ['CK_HERMES_BUS'], 'a') as stream:
        stream.write(late + '\\n')
release = os.environ.get('CK_HERMES_RELEASE')
deadline = time.monotonic() + 30
while release and not Path(release).exists() and time.monotonic() < deadline:
    time.sleep(.02)
if fail:
    print(json.dumps(dict(type='result', status='failed', error='provider said no')))
    sys.exit(2)
print(json.dumps(dict(type='result', status='success', result='done', session_id='sess-1')))
'''


def row(rid, to='ops', kind='ask', frm='lead'):
    return json.dumps({'ts': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'), 'id': rid,
                       'frm': frm, 'to': to, 'topic': 'general', 'kind': kind, 'text': 'please handle ' + rid})


class HermesCase(FakeCase):
    def setUp(self):
        super().setUp()
        self.file.write_text(BASE + LANE)
        self.manifest = load(self.file)
        bus.configure(self.manifest)
        self.lane = self.manifest.lanes[0]
        Path(self.lane.cwd).mkdir()
        Path(self.lane.config_dir).mkdir()
        self.manifest.paths.bus_log.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.paths.bus_log.touch()
        self.calls = self.root/'hermes-calls.jsonl'
        binary = self.root/'fakes'/'hermes'
        binary.write_text('#!' + sys.executable + ' -S\n' + FAKE)
        binary.chmod(0o755)
        env = patch.dict(os.environ, HERMES=str(binary), CK_HERMES_LOG=str(self.calls),
                         CK_HERMES_BUS=str(self.manifest.paths.bus_log), COORDKIT_AS='')
        env.start(); self.addCleanup(env.stop)
        probe = patch.object(addressed, 'PROBE', lambda: set())
        probe.start(); self.addCleanup(probe.stop)

    def attach(self):
        stat = self.manifest.paths.bus_log.stat()
        write_json(deliver.cursor_path(self.manifest, 'ops'), {'file': [stat.st_dev, stat.st_ino], 'offset': 0})

    def post(self, *rows):
        with self.manifest.paths.bus_log.open('a') as stream:
            for line in rows:
                stream.write(line + '\n')

    def turn(self):
        return hermes_turn.main(['--manifest', str(self.file), '--lane', 'ops', '--key', 'k1'])

    def invocations(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []


class ManifestTests(HermesCase):
    def load_with(self, old, new):
        self.file.write_text((BASE + LANE).replace(old, new))
        return load(self.file)

    def test_hermes_lane_needs_one_profile_and_no_claude_resume_fields(self):
        self.assertEqual(self.lane.profile, 'ops')
        for old, new in (('profile = "ops"\n', ''), ('profile = "ops"', 'profile = "Bad Name"'),
                         ('profile = "ops"', 'profile = "ops"\nsession_id = "abc"'),
                         ('profile = "ops"', 'profile = "ops"\nresume = "headless"'),
                         ('profile = "ops"', 'profile = "ops"\nturn_timeout = "5s"'),
                         ('harness = "hermes"\n', '')):
            with self.subTest(new=new), self.assertRaises(ManifestError):
                self.load_with(old, new)
        twin = LANE.partition('[[lanes]]')[2].replace('name = "ops"', 'name = "ops2"')
        self.file.write_text(BASE + LANE + '[[lanes]]' + twin)
        with self.assertRaisesRegex(ManifestError, 'one hermes lane per profile'):
            load(self.file)

    def test_adapter_is_discovered_and_reports_missing_profile_or_hook(self):
        adapter = next(a for a in available(self.manifest) if a.name == 'hermes')
        self.assertEqual(adapter.status(), 'lanes=ops; no turn-start hook for ops')
        (Path(self.lane.config_dir)/'config.yaml').write_text('hooks:\n  pre_llm_call:\n  - command: coordkit deliver --hook\n')
        self.assertEqual(adapter.status(), 'lanes=ops')
        Path(self.lane.config_dir, 'config.yaml').unlink()
        Path(self.lane.config_dir).rmdir()
        with self.assertRaisesRegex(ToolError, 'profile_home_missing'):
            adapter.status()
        with patch.dict(os.environ, HERMES=str(self.root/'absent')), self.assertRaisesRegex(ToolError, 'binary_unavailable'):
            adapter.status()


class TurnTests(HermesCase):
    def test_turn_runs_in_the_profile_session_with_rows_and_records_usage(self):
        self.attach()
        self.post(row('a1'), row('other', to='someone-else'), row('fyi1', kind='fyi'))
        self.assertEqual(self.turn(), 0)
        calls = self.invocations()
        self.assertEqual(len(calls), 1)
        argv = calls[0]['argv']
        self.assertEqual(argv[:8], ['-p', 'ops', '--in', self.lane.cwd, '--no-restore-cwd', '--accept-hooks',
                                    '--output-format', 'json'])
        self.assertNotIn('--resume', argv)
        self.assertEqual(hermes.session_id(self.lane), 'sess-1')
        self.assertEqual(Path(str(self.calls) + '.admin').read_text().strip(),
                         json.dumps(['-p', 'ops', 'sessions', 'rename', 'sess-1', 'ops']))
        self.assertEqual(argv[argv.index('-m') + 1], 'vendor-model')
        self.assertNotIn('--reasoning', argv)
        self.assertEqual(argv[-2], '-z')
        self.assertIn('Your bus name is ops', argv[-1])
        self.assertIn('--re a1', argv[-1])
        self.assertNotIn('other', argv[-1])
        self.assertEqual(calls[0]['as_'], 'ops')
        self.assertEqual(calls[0]['cwd'], self.lane.cwd)
        receipt = hermes.last_receipt(self.lane)
        self.assertTrue(receipt['ok'])
        self.assertEqual((receipt['rows'], receipt['session_id'], receipt['model']), (1, 'sess-1', 'vendor-model'))
        self.assertEqual(receipt['usage'], {'input_tokens': 100, 'output_tokens': 7, 'cache_read_input_tokens': 30,
                                            'cache_creation_input_tokens': 5, 'reasoning_tokens': 2, 'api_calls': 3})
        # Rows handed over in the prompt are delivered: an in-turn hook must not repeat them.
        self.assertEqual(read_json(deliver.cursor_path(self.manifest, 'ops'))['offset'],
                         self.manifest.paths.bus_log.stat().st_size)

    def test_rows_that_arrive_during_a_turn_start_the_next_round(self):
        """Contract: a lane with no waiter must not end while addressed work is unread."""
        self.attach()
        with patch.dict(os.environ, CK_HERMES_LATE_ROW=row('late1')):
            self.assertEqual(self.turn(), 0)
        calls = self.invocations()
        self.assertEqual(len(calls), 2)
        self.assertNotIn('late1', calls[0]['argv'][-1])
        self.assertEqual(calls[1]['argv'][6:8], ['--resume', 'sess-1'])  # the same session carries on
        self.assertIn('--re late1', calls[1]['argv'][-1])
        self.assertIn('More addressed work arrived', calls[1]['argv'][-1])
        self.assertEqual([r['round'] for r in hermes.receipts(self.lane)[0]], [0, 1])

    def test_first_attachment_skips_history_and_a_failed_turn_stops_the_rounds(self):
        self.post(row('old'))
        with patch.dict(os.environ, CK_HERMES_FAIL='1', CK_HERMES_LATE_ROW=row('late2')):
            self.assertEqual(self.turn(), 2)
        calls = self.invocations()
        self.assertEqual(len(calls), 1)
        self.assertNotIn('--re old', calls[0]['argv'][-1])
        receipt = hermes.last_receipt(self.lane)
        self.assertFalse(receipt['ok'])
        self.assertEqual(receipt['error'], 'provider said no')
        self.assertEqual(addressed.listening(self.manifest, 'ops'), (False, 'hermes turn failed: provider said no'))

    def test_a_session_that_no_longer_resumes_is_replaced_once(self):
        hermes.remember_session(self.lane, 'gone-1')
        with patch.dict(os.environ, CK_HERMES_GONE='gone-1'):
            self.assertEqual(self.turn(), 0)
        calls = self.invocations()
        self.assertEqual(len(calls), 2)
        self.assertIn('gone-1', calls[0]['argv'])
        self.assertNotIn('--resume', calls[1]['argv'])
        self.assertEqual(hermes.session_id(self.lane), 'sess-1')
        self.assertEqual(len(hermes.receipts(self.lane)[0]), 1)

    def test_a_resumed_turn_that_dies_is_never_repeated(self):
        """Contract: only a session Hermes reports missing is replaced; a crash may have done work."""
        hermes.remember_session(self.lane, 'live-1')
        with patch.dict(os.environ, CK_HERMES_CRASH='live-1'):
            self.assertEqual(self.turn(), 1)
        self.assertEqual(len(self.invocations()), 1)
        self.assertEqual(hermes.session_id(self.lane), 'live-1')
        self.assertFalse(hermes.last_receipt(self.lane)['ok'])

    def test_a_plain_message_posted_mid_turn_still_gets_a_round(self):
        """Contract: the re-check covers every row the wake path would have acted on."""
        self.attach()
        with patch.dict(os.environ, CK_HERMES_LATE_ROW=row('m1', kind='msg')):
            self.assertEqual(self.turn(), 0)
        calls = self.invocations()
        self.assertEqual(len(calls), 2)
        self.assertIn('BUS msg m1', calls[1]['argv'][-1])
        targets = deliver.targets_for(self.manifest, 'ops')
        for quiet in (row('f', kind='fyi'), row('mine', frm='ops'), row('elsewhere', to='other', kind='msg'),
                      json.dumps(dict(json.loads(row('ack1', kind='ack')), re='x'))):
            self.assertFalse(hermes_turn.wants_turn(json.loads(quiet), 'ops', targets), quiet)

    def test_a_second_runner_yields_to_the_lock_holder(self):
        with hermes.turn_lock(self.lane) as held:
            self.assertIsNotNone(held)
            self.assertTrue(hermes.turn_running(self.lane))
            self.assertEqual(self.turn(), 0)
        self.assertEqual(self.invocations(), [])
        self.assertFalse(hermes.turn_running(self.lane))


class WakeTests(HermesCase):
    def setUp(self):
        super().setUp()
        fast = patch.object(hermes, 'REFUSAL_SECONDS', .3)
        fast.start(); self.addCleanup(fast.stop)
        self.adapter = Hermes(self.manifest)
        self.release = self.root/'release'

    def wait_until(self, check, timeout=10):
        deadline = time.monotonic() + timeout
        while not check() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(check())

    def test_wake_detaches_a_runner_and_listening_follows_its_lock(self):
        self.attach()
        self.post(row('w1'))
        with patch.dict(os.environ, CK_HERMES_RELEASE=str(self.release)):
            result = self.adapter.wake(self.manifest, 'ops', [], 'key-1')
            self.assertEqual((result['status'], result['profile']), ('woken', 'ops'))
            self.wait_until(lambda: hermes.turn_running(self.lane))
            self.assertTrue(self.adapter.listening(self.manifest, 'ops', []))
            self.assertTrue(wake.is_listening(self.manifest, 'ops', []))
            self.assertEqual(addressed.listening(self.manifest, 'ops'), (True, 'hermes turn running'))
            self.assertEqual(self.adapter.wake(self.manifest, 'ops', [], 'key-2')['status'], 'listening')
            self.release.touch()
            self.wait_until(lambda: not hermes.turn_running(self.lane))
        self.assertEqual(len(self.invocations()), 1)
        self.assertEqual(hermes.last_receipt(self.lane)['key'], 'key-1')
        self.assertFalse(self.adapter.listening(self.manifest, 'ops', []))
        self.assertEqual(addressed.listening(self.manifest, 'ops'), (True, 'hermes wake-on-demand lane'))

    def test_a_finished_turn_lifts_the_debounce_but_a_failed_one_keeps_it(self):
        """Contract: a row posted just after a clean turn ended still starts a turn."""
        record = {'at': time.time() - 1, 'lane': 'ops', 'key': 'k', 'status': 'woken', 'attempts': []}
        write_json(wake.wake_path(self.manifest, 'ops'), record)
        self.assertTrue(wake.is_listening(self.manifest, 'ops', []))  # woken, no receipt yet
        hermes.append_receipt(self.lane, {'at': time.time(), 'ended_at': time.time(), 'ok': False, 'usage': {}})
        self.assertFalse(hermes.settled(self.manifest, 'ops', record))
        self.assertTrue(wake.is_listening(self.manifest, 'ops', []))
        hermes.append_receipt(self.lane, {'at': time.time(), 'ended_at': time.time(), 'ok': True, 'usage': {}})
        self.assertTrue(hermes.settled(self.manifest, 'ops', record))
        self.assertFalse(wake.is_listening(self.manifest, 'ops', []))

    def test_notify_wakes_only_the_hermes_adapter_path_and_records_failure(self):
        other = Hermes(self.manifest)
        other.name = 'claude-code'  # stands in for any other harness: it must not be tried
        other.wake = lambda *args: self.fail('a hermes lane was offered to another harness')
        with patch('coordkit.wake.wake_adapters', return_value=[other, self.adapter]), \
                patch.dict(os.environ, HERMES=str(self.root/'absent')):
            wake.notify(self.manifest, [json.loads(row('n1'))])
        record = wake.wake_record(self.manifest, 'ops')
        self.assertEqual(record['status'], 'unresumable')
        self.assertEqual([a['adapter'] for a in record['attempts']], ['hermes'])
        live, evidence = addressed.listening(self.manifest, 'ops')
        self.assertFalse(live)
        self.assertIn('hermes binary unavailable', evidence)

    def test_other_lanes_are_untouched(self):
        self.assertIsNone(wake.on_demand(self.manifest, 'worker'))
        self.assertEqual(self.adapter.wake(self.manifest, 'worker', [], 'k'),
                         {'status': 'unresumable', 'reason': 'no hermes lane'})
        self.assertFalse(self.adapter.listening(self.manifest, 'worker', []))


class UsageAndDeliveryTests(HermesCase):
    def test_usage_sums_receipts_in_the_window_and_reports_the_model(self):
        now = time.time()
        for ended, model in ((now - 7200, 'old-model'), (now - 60, 'vendor-model'), (now - 30, 'vendor-model')):
            hermes.append_receipt(self.lane, {'at': ended - 5, 'ended_at': ended, 'ok': True, 'model': model,
                                              'usage': {'input_tokens': 10, 'output_tokens': 2,
                                                        'cache_read_input_tokens': 4, 'api_calls': 2}})
        with hermes.receipts_path(self.lane).open('a') as stream:
            stream.write('not json\n')
        at = dt.datetime.fromtimestamp(now, dt.timezone.utc)
        result = usage.read_lane(self.lane, at - dt.timedelta(hours=1), at)
        self.assertEqual((result.turns, result.input_tokens, result.output_tokens, result.cache_read_input_tokens),
                         (4, 20, 4, 8))
        self.assertEqual((result.models, result.invalid_rows), (['vendor-model'], 1))
        self.assertEqual(usage.hermes_observed_model(self.lane)[0], 'vendor-model')
        self.assertEqual(usage.doctor_warnings(self.manifest), [])
        hermes.append_receipt(self.lane, {'at': now - 5, 'ended_at': now - 1, 'ok': True, 'model': 'surprise', 'usage': {}})
        self.assertEqual(usage.doctor_warnings(self.manifest), ['lane=ops intended=vendor-model actual=surprise'])
        self.lane.model = 'default'  # the profile chooses; whatever it ran is not drift
        self.assertEqual(usage.doctor_warnings(self.manifest), [])

    def test_hook_block_is_owned_reversible_and_never_edits_a_foreign_hooks_map(self):
        config = Path(self.lane.config_dir)/'config.yaml'
        config.write_text('model:\n  provider: vendor\n')
        self.assertEqual(hermes.manage_hook(self.manifest, 'ops', 'status'), 'absent')
        self.assertEqual(hermes.manage_hook(self.manifest, 'ops', 'install'), 'OK install')
        self.assertEqual(hermes.manage_hook(self.manifest, 'ops', 'install'), 'OK install')  # reconciles in place
        text = config.read_text()
        self.assertEqual(text.count('pre_llm_call'), 1)
        self.assertIn(' deliver --hook', text)
        self.assertTrue(hermes.hook_installed(self.lane))
        self.assertEqual(hermes.manage_hook(self.manifest, 'ops', 'status'), 'installed coordkit-owned')
        config.write_text(text.replace('timeout: 10', 'timeout: 30'))  # someone edits the managed block
        self.assertEqual(hermes.manage_hook(self.manifest, 'ops', 'status'), 'drift')
        for action in ('uninstall', 'install'):
            with self.assertRaisesRegex(ManifestError, 'hermes_hook_conflict'):
                hermes.manage_hook(self.manifest, 'ops', action)
        self.assertIn('timeout: 30', config.read_text())
        config.write_text(text)
        self.assertEqual(hermes.manage_hook(self.manifest, 'ops', 'uninstall'), 'OK uninstall')
        self.assertEqual(config.read_text(), 'model:\n  provider: vendor\n')
        self.assertEqual(hermes.manage_hook(self.manifest, 'ops', 'uninstall'), 'absent')
        config.write_text('hooks:\n  pre_tool_call: []\n')
        with self.assertRaisesRegex(ManifestError, 'hermes_hooks_present'):
            hermes.manage_hook(self.manifest, 'ops', 'install')
        self.assertEqual(config.read_text(), 'hooks:\n  pre_tool_call: []\n')
        with self.assertRaisesRegex(ManifestError, 'not_a_hermes_lane'):
            hermes.manage_hook(self.manifest, 'worker', 'install')

    def test_turn_start_hook_answers_in_the_hermes_dialect_and_shares_the_cursor(self):
        self.attach()
        self.post(row('h1'))
        out = io.StringIO()
        payload = {'hook_event_name': 'pre_llm_call', 'session_id': 'sess-1', 'profile': 'ops'}
        # No COORDKIT_AS: a turn started by hand in the profile is identified by the payload
        # together with the profile home Hermes exports. The payload alone names nobody.
        with patch.dict(os.environ, HERMES_HOME=str(self.root/'another-home')):
            deliver.run_hook(self.manifest, json.dumps(payload), out)
        self.assertEqual(out.getvalue(), '')
        with patch.dict(os.environ, HERMES_HOME=self.lane.config_dir):
            self.assertEqual(deliver.run_hook(self.manifest, json.dumps(payload), out), 0)
            self.assertIn('--from ops --re h1', json.loads(out.getvalue())['context'])
            again = io.StringIO()
            deliver.run_hook(self.manifest, json.dumps(payload), again)
            self.assertEqual(again.getvalue(), '')
            self.post(row('h2'))
            stranger = io.StringIO()
            deliver.run_hook(self.manifest, json.dumps(dict(payload, profile='someone-else')), stranger)
            self.assertEqual(stranger.getvalue(), '')
        self.assertEqual(len(hermes_turn.pending(self.manifest, 'ops')[2]), 1)  # h2 stays for the lane
