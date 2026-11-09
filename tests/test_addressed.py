"""Addressed routing, renewable listening evidence and bounded receipt escalation."""
import contextlib
import datetime as dt
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

from coordkit import addressed, bus, sidecar
from coordkit.lifecycle import shadow_allowed
from coordkit.manifest import load
from tests.support import BASE, FakeCase

SRC = Path(__file__).resolve().parents[1]/'src'

ADDRESSED = '''
[addressed]
lease_ttl = "90s"
rearm_grace = "3m"
heartbeat_every = "1s"
confirm_timeout = "1s"
relay = "relay"
relay_file = "pending-alerts.ndjson"
[addressed.roles]
coordinator = ["coordinator", "lead-a"]
[addressed.agents.lead-a]
role = "lead"
runtime = "claude-code"
fallback = "coordinator"
[addressed.agents.coordinator]
runtime = "claude-code"
fallback = "relay"
[addressed.agents.relay]
runtime = "claude-code"
[addressed.agents.worker-ui]
runtime = "codex"
adapter = "tmux"
fallback = "lead-a"
'''


def ts(at):
    return dt.datetime.fromtimestamp(at, dt.timezone.utc).isoformat(timespec='seconds')


class AddressedCase(FakeCase):
    def setUp(self):
        super().setUp()
        self.file.write_text(BASE + ADDRESSED)
        self.manifest = load(self.file)
        self.manifest.escalation.internal_senders = ['service-agent']
        bus.configure(self.manifest)
        probe = patch.object(addressed, 'PROBE', lambda: set())
        probe.start(); self.addCleanup(probe.stop)
        self.now = time.time()

    # helpers ---------------------------------------------------------------------------------------
    def lease(self, name, heartbeat_ago=0.0, ended_ago=None, reason='timeout'):
        data = {'name': name, 'lease_id': f'{name}-{heartbeat_ago}-{ended_ago}', 'pid': 1, 'host': 'h',
                'started_at': self.now - heartbeat_ago - 60, 'heartbeat_at': self.now - heartbeat_ago}
        if ended_ago is not None:
            data.update(ended_at=self.now - ended_ago, end_reason=reason)
        addressed.write_json(addressed.lease_path(self.manifest, name), data)

    def adapter_lease(self, name):
        # The adapter reads legacy lease files; the removed waiter no longer creates them.
        self.lease(name)
        lease = addressed.Lease(self.manifest, name)
        lease.data = addressed.read_lease(self.manifest, name)
        return lease

    def incumbent_row(self, id, frm, to, kind='ask', text='go?', ago=60.0, **extra):
        """An external bus row without coordkit metadata."""
        rec = dict(ts=ts(self.now - ago), id=id, frm=frm, to=to, topic='general', kind=kind, text=text, **extra)
        self.manifest.paths.bus_log.parent.mkdir(parents=True, exist_ok=True)
        with open(self.manifest.paths.bus_log, 'a') as f:
            f.write(json.dumps(rec) + '\n')
        return rec

    def rows(self):
        return bus.read()

    def digest_text(self):
        return '\n'.join(bus.line(r, full=True) for r in self.rows() if r.get('topic') == 'coordkit-digest')

    def relay_lines(self):
        path = self.manifest.path('pending-alerts.ndjson')
        return [json.loads(l)['text'] for l in path.read_text().splitlines()] if path.exists() else []


    def quiet(self, fn, *args, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = fn(*args, **kwargs)
        return result, out.getvalue() + err.getvalue()

    def sweep(self, at=None, **kw):
        state = addressed.home(self.manifest)/'sweep.json'
        if not state.exists():  # adopted an hour ago; rows before adoption are the incumbent's backlog
            addressed.write_json(state, {'baseline': self.now - 3600, 'seen': {}})
        lines = []
        found = addressed.sweep(self.manifest, at=self.now if at is None else at, out=lines.append, **kw)
        return found, lines


class AddressedTests(AddressedCase):
    # -- the four required tests (section 8.6) -------------------------------------------------------

    def test_8b_role_with_no_live_member_alerts_relay_and_human(self):
        self.lease('relay')
        self.lease('lead-a', heartbeat_ago=3600)   # lapsed an hour ago
        rc, out = self.quiet(addressed.send, self.manifest, 'worker-remote', 'coordinator', 'go/no-go on Step B?')
        self.assertEqual(rc, addressed.EXIT_VACANT)
        self.assertIn('ROLE_VACANT coordinator', out)
        rows = self.rows()
        # Nothing silently copied to the lead; the relay is told (bus row + relay file) and the human paged.
        self.assertFalse([r for r in rows if r['to'] == 'lead'])
        self.assertEqual(rows[0]['to'], 'coordinator')
        self.assertFalse([r for r in rows if r.get('escalation')])
        self.assertIn('ROLE_VACANT coordinator', (addressed.home(self.manifest)/'lead-digest.ndjson').read_text())
        # The sweep reports the vacancy even when nobody sends anything.
        found, _ = self.sweep(at=time.time())
        self.assertIn('role_vacant', [f['event'] for f in found])
        # With a live member, the role send goes to that member and must be acked by it.
        self.lease('lead-a')
        rc, _ = self.quiet(addressed.send, self.manifest, 'worker-remote', 'coordinator', 'second ask')
        self.assertEqual((rc, self.rows()[-1]['to'], self.rows()[-1]['role']), (0, 'lead-a', 'coordinator'))

    def test_8c_sidecar_typing_into_a_pane_that_does_not_submit_reports_delivery_unconfirmed(self):
        pane, log = self.root/'pane.txt', self.root/'tmux.jsonl'
        pane.write_text('claude code\n> \n')
        self.lease('lead-a')
        env = patch.dict(os.environ, TMUX_BIN=str(self.root/'fakes/tmux'), FAKE_TMUX_PANE=str(pane), FAKE_TMUX_LOG=str(log))
        env.start(); self.addCleanup(env.stop)
        row = self.incumbent_row('c1', 'lead-a', 'worker-ui', text='rebase #12346 please')
        lease = self.adapter_lease('worker-ui')
        result, out = self.quiet(sidecar.deliver, self.manifest, 'worker-ui', 'pane:0', row, lease)
        self.assertEqual(result, 'delivery_unconfirmed')
        typed = [json.loads(l) for l in log.read_text().splitlines()]
        self.assertEqual([a[-1] for a in typed if a[0] == 'send-keys'][1], 'Enter')
        # Enter was sent but nothing confirmed: escalated, and the agent now counts as deaf until it acks.
        self.assertIn('DELIVERY_UNCONFIRMED worker-ui ask c1', out)
        self.assertEqual([r['to'] for r in self.rows() if r.get('escalation') == 'delivery_unconfirmed'], ['lead-a'])
        self.assertEqual(addressed.listening(self.manifest, 'worker-ui')[0], False)
        # The pointer is still sitting in the input box: the next delivery must not type over it.
        before = len(log.read_text().splitlines())
        row2 = self.incumbent_row('c2', 'lead-a', 'worker-ui', text='second')
        result, out = self.quiet(sidecar.deliver, self.manifest, 'worker-ui', 'pane:0', row2, lease)
        self.assertEqual(result, 'input_busy')
        self.assertFalse([l for l in log.read_text().splitlines()[before:] if '"send-keys"' in l])
        # c2 was never typed: escalated, retryable, and it does not make the agent deaf by itself.
        self.assertNotIn('c2', addressed.read_lease(self.manifest, 'worker-ui')['unconfirmed'])
        # An ack of the attempted delivery clears its deafness.
        self.quiet(addressed.ack, self.manifest, 'c1', 'worker-ui', 'received')
        self.assertTrue(addressed.listening(self.manifest, 'worker-ui')[0])

    def test_an_unacked_ask_never_escalates_back_to_its_own_recipient(self):

        for name in ('worker-ui', 'lead-a', 'coordinator'):
            self.lease(name, heartbeat_ago=-3*3600)
        self.incumbent_row('c1', 'worker-ui', 'coordinator', ago=40*60)
        self.sweep()
        self.assertFalse([r for r in self.rows() if r.get('re') == 'c1'])
        self.assertIn('c1', self.digest_text())


    def test_lead_escalations_off_batch_into_one_hourly_digest(self):

        self.manifest.addressed.lead_escalations = False
        for name in ('worker-ui', 'lead-a', 'coordinator'):
            self.lease(name, heartbeat_ago=-3*3600)
        self.incumbent_row('d1', 'coordinator', 'worker-ui', ago=40*60)
        self.incumbent_row('c1', 'coordkit', 'worker-ui', ago=40*60)
        found, _ = self.sweep()
        self.assertEqual([f['item']['id'] for f in found], ['d1'])
        self.assertIn('d1', self.digest_text())
        self.assertNotIn('ask c1 ', self.digest_text())  # a random row id can contain 'c1'
        self.sweep(at=self.now+10*60)
        self.assertEqual(len([r for r in self.rows() if r.get('topic') == 'coordkit-digest']), 1)
        self.sweep(at=self.now+70*60)
        digests = [r for r in self.rows() if r.get('topic') == 'coordkit-digest']
        self.assertEqual(len(digests), 2)
        self.assertTrue(all(r['frm'] == 'coordkit' and r['kind'] == 'fyi' for r in digests))


    def test_machine_finding_for_the_lead_goes_to_the_digest(self):
        from coordkit import escalation
        self.manifest.addressed.lead_escalations = False
        self.manifest.escalation.card = 'card:task'
        result = escalation.ask(self.manifest, 'card:task', kind='rulings', situation='Ruling r1 is unacked.',
                                impact='i', recommend='Chase the owner.', why='w')
        self.assertEqual(result['routed_to'], 'lead_digest')
        asks = [r for r in self.rows() if r['frm'] == 'coordkit' and r['kind'] == 'ask']
        self.assertEqual(len(asks), 1)
        self.assertTrue(asks[0]['digest_only'])  # durable question, still no individual wake
        self.assertIn('finding:rulings', (addressed.home(self.manifest)/'lead-digest.ndjson').read_text())

    def test_8d_awake_agent_ignoring_its_inbox_is_swept(self):

        for name in ('worker-ui', 'lead-a', 'coordinator'):
            self.lease(name, heartbeat_ago=-24*3600)
        self.incumbent_row('d1', 'coordinator', 'worker-ui', ago=14*60)
        self.assertFalse(self.sweep()[0])
        found, _ = self.sweep(at=self.now+2*60)
        self.assertEqual([f['event'] for f in found], ['pickup'])
        wakes = [r for r in self.rows() if r.get('re') == 'd1']
        self.assertEqual([(r['frm'], r['to'], r['kind']) for r in wakes], [('coordkit','worker-ui','handoff')])
        self.assertFalse(self.sweep(at=self.now+10*60)[0])
        self.quiet(addressed.ack, self.manifest, 'd1', 'worker-ui', 'received')
        self.assertFalse(self.sweep(at=self.now+3*3600)[0])


    def test_lease_semantics_alive_process_never_counts(self):
        self.assertEqual(addressed.listening(self.manifest, 'lead-a'), (False, 'no waiter lease'))
        self.lease('lead-a', heartbeat_ago=91)  # renewed 91s ago, still "running" (no ended_at): wedged
        live, why = addressed.listening(self.manifest, 'lead-a')
        self.assertFalse(live)
        self.assertIn('lapsed', why)
        with patch.object(addressed, 'PROBE', lambda: {'lead-a'}):
            self.assertFalse(addressed.listening(self.manifest, 'lead-a')[0], 'probe is opt-in')
            self.manifest.addressed.incumbent_waiters = True
            self.assertEqual(addressed.listening(self.manifest, 'lead-a')[1], 'incumbent bus wait process (pre-cutover probe)')


    def test_ack_rows_are_incumbent_answers_and_close_obligations(self):
        from coordkit import obligations
        self.incumbent_row('q1', 'coordinator', 'lead-a')
        self.quiet(addressed.ack, self.manifest, 'q1', 'lead-a', 'done', 'merged #1')
        ack = self.rows()[-1]
        self.assertEqual((ack['kind'], ack['re'], ack['ack'], ack['text'], ack['to']), ('answer', 'q1', 'done', 'done: merged #1', 'coordinator'))
        self.assertEqual(obligations.open_obligations(self.manifest, self.rows(), time.time()), [])


    def test_bus_post_to_a_registered_name_never_copies_the_lead(self):
        # Before: `coordkit bus post` to a silent recipient appended "[copy: X silent >3h]" to the lead.
        self.lease('coordinator')
        _, out = self.quiet(bus.main, ['post', '--from', 'x', '--to', 'lead-a', '--kind', 'ask', 'are you there?'], self.manifest)
        rows = self.rows()
        self.assertFalse([r for r in rows if r['to'] == 'lead' or '[copy:' in r['text']])
        self.assertFalse([r for r in rows if r.get('escalation')])
        self.assertIn('recipient_deaf', (addressed.home(self.manifest)/'lead-digest.ndjson').read_text())
        self.assertIn('RECIPIENT_DEAF lead-a', out)

    def test_deaf_fyi_is_reported_but_not_escalated(self):
        rc, out = self.quiet(addressed.send, self.manifest, 'x', 'lead-a', 'fyi only', kind='fyi')
        self.assertEqual(rc, addressed.EXIT_DEAF)
        self.assertEqual(len(self.rows()), 1)

    def test_shadow_policy(self):
        self.assertTrue(all(shadow_allowed(c, a) for c, a in
                            [('sweep', []), ('wait', ['pending', '--brief']), ('wait', ['ask-guard'])]))
        self.assertFalse(any(shadow_allowed(c, a) for c, a in [('send', ['--to', 'x']), ('ack', ['x']), ('sidecar', [])]))
        self.assertFalse(shadow_allowed('wait', ['wait', '--key', 'lead']))

    def test_shadow_sweep_reaches_only_the_relay_file(self):

        self.manifest.mode = 'shadow'
        self.lease('coordinator')
        self.incumbent_row('s1', 'x', 'lead-a', text='URGENT now', ago=30)
        self.sweep()
        self.assertEqual([r['id'] for r in self.rows()], ['s1'])
        self.assertIn('s1', '\n'.join(self.relay_lines()))


    def test_manifest_validation(self):
        from coordkit.manifest import ManifestError
        for bad in ('[addressed.roles]\nx = []\n', '[addressed.agents.x]\nrole = 1\n',
                    '[addressed]\nlease_ttl = "soon"\n', '[addressed]\npage_wake = ["page"]\n'):
            self.file.write_text(BASE + bad)
            with self.subTest(bad=bad), self.assertRaises(ManifestError):
                load(self.file)


    # -- decoupling (brief section 5) ----------------------------------------------------------------
    def test_no_agentctl_dependency(self):
        for name in ('addressed.py', 'sidecar.py'):
            source = (SRC/'coordkit'/name).read_text()
            self.assertNotIn('actl', source.replace('# ', ''))
            self.assertNotIn('agentctl', source.split('"""', 2)[2])


    def test_rows_stay_readable_by_the_incumbent_bus(self):

        self.lease('coordinator')
        self.incumbent_row('readable', 'x', 'worker-ui', ago=3600)
        self.sweep()
        rows = self.rows()
        self.assertTrue(all(isinstance(r['to'], str) for r in rows))
        # A stdlib-only reader sees scalar rows with the incumbent's required fields.
        parsed = [json.loads(l) for l in self.manifest.paths.bus_log.read_text().splitlines()]
        self.assertTrue(all({'id','ts','frm','to','kind','text'} <= set(r) for r in parsed))


    def test_normal_message_to_a_briefly_deaf_agent_waits_for_rearm_grace(self):

        self.manifest.addressed.incumbent_waiters = True
        self.incumbent_row('n1', 'x', 'worker-ui', ago=30)
        with patch.object(addressed, 'PROBE', lambda: set(addressed.registry(self.manifest))):
            self.assertFalse(self.sweep()[0])
            self.assertFalse(self.sweep(at=self.now+4*60)[0])
            found, _ = self.sweep(at=self.now+16*60)
        self.assertEqual([f['event'] for f in found], ['pickup'])


    def test_failed_waiter_gets_no_rearm_grace(self):
        for reason in ('signal 15', 'error TimeoutError'):
            self.lease('lead-a', heartbeat_ago=10, ended_ago=5, reason=reason)
            self.assertFalse(addressed.listening(self.manifest, 'lead-a')[0], reason)
        self.lease('lead-a', heartbeat_ago=10, ended_ago=5, reason='delivered')
        self.assertTrue(addressed.listening(self.manifest, 'lead-a')[0])

    def test_bystander_reply_does_not_stop_the_receipt_sweep(self):
        for name in ('worker-ui', 'lead-a', 'coordinator'):
            self.lease(name, heartbeat_ago=-3600)
        self.incumbent_row('b1', 'coordinator', 'worker-ui', ago=16*60)
        _, out = self.quiet(addressed.ack, self.manifest, 'b1', 'worker-a', 'received')
        self.assertIn('only the assignee', out)
        self.assertEqual([f['event'] for f in self.sweep()[0]], ['pickup'])


    def test_first_sweep_covers_the_recent_backlog(self):

        for name in ('coordinator', 'relay', 'lead-a'):
            self.lease(name, heartbeat_ago=-24*3600)
        self.incumbent_row('r1', 'x', 'coordinator', ago=20*60)
        self.incumbent_row('r0', 'x', 'coordinator', ago=7*3600)
        self.sweep()
        self.assertIn('r1', self.digest_text())
        self.assertIn('r0', self.digest_text())
        self.assertFalse([r for r in self.rows() if r['id'].startswith(('wake-ob-', 'alert-ob-'))])


    def test_blocker_escalation_to_relay_is_digest_only(self):
        self.lease('coordinator')
        self.quiet(bus.main, ['post', '--from', 'x', '--to', 'lead-a', '--kind', 'blocker', '--owner', 'lead-a',
                              '--needs', 'a decision', 'stuck'], self.manifest)
        self.assertFalse([r for r in self.rows() if r.get('escalation')])
        self.assertIn('blocker', (addressed.home(self.manifest)/'lead-digest.ndjson').read_text())
        self.assertEqual(len(bus.open_blockers(self.rows())), 1)

    def test_mirror_failure_cannot_stop_the_deaf_escalation(self):
        self.file.write_text(self.file.read_text().replace('live_check = false', 'live_check = false\nmirror = true'))
        self.manifest = load(self.file)
        self.lease('coordinator')
        from coordkit.process import ToolError
        with patch('coordkit.adapters.builtin.Anx.project', side_effect=ToolError('anx', 6, 'down', 'dependency_unavailable')):
            with self.assertRaises(SystemExit):
                self.quiet(addressed.send, self.manifest, 'x', 'lead-a', 'URGENT ping', urgent=True)
        self.assertFalse([r for r in self.rows() if r.get('escalation')])
        self.assertIn('recipient_deaf', (addressed.home(self.manifest)/'lead-digest.ndjson').read_text())
        self.assertIn('recipient_deaf', (addressed.home(self.manifest)/'lead-digest.ndjson').read_text())

    def test_incumbent_probe_matches_the_bus_process_not_shell_wrappers(self):
        py = '/opt/homebrew/Cellar/python@3.14/3.14.6/Frameworks/Python.framework/Versions/3.14/Resources/Python.app/Contents/MacOS/Python'
        cases = {f'{py} ./bin/bus wait --to worker-remote --timeout 9': 'worker-remote',
                 f'{py} /x/swarm/bin/bus wait --to=worker-search': 'worker-search',
                 '/x/swarm/bin/bus wait --to lead-a': 'lead-a',
                 "/bin/zsh -c 'cd swarm && ./bin/bus wait --to lead-a --timeout 9'": None,
                 'bash -c "bus wait --to lead-a"': None,
                 f'{py} ./bin/bus inbox lead-a': None,
                 'grep bus wait --to lead-a': None}
        for args, want in cases.items():
            self.assertEqual(addressed.incumbent_listener(args), want, args)


def iter_clock(start, step=1.0):
    state = {'t': start}
    def clock():
        state['t'] += step
        return state['t']
    return clock


class AddressedReview2Tests(AddressedCase):
    def test_backlog_to_a_deaf_agent_gets_one_wake(self):

        for name in ('coordinator', 'relay'):
            self.lease(name, heartbeat_ago=-24*3600)
        self.incumbent_row('old', 'x', 'worker-ui', ago=40*60)
        self.incumbent_row('ancient', 'x', 'coordinator', ago=7*3600)
        self.sweep()
        self.sweep(at=self.now+4*60)
        self.assertEqual(len([r for r in self.rows() if r.get('re') == 'old']), 1)
        self.assertIn('ancient', self.digest_text())


class ConfirmationExpiryTests(AddressedCase):
    def test_unconfirmed_expires_without_overriding_other_listening_evidence(self):
        self.manifest.addressed.confirm_timeout = '20m'
        self.manifest.addressed.incumbent_waiters = True
        self.lease('worker-ui')
        lease = addressed.read_lease(self.manifest, 'worker-ui')
        lease['unconfirmed'] = ['old', 'fresh']
        lease['unconfirmed_at'] = {'old': self.now - 38*3600, 'fresh': self.now - 60}
        addressed.write_json(addressed.lease_path(self.manifest, 'worker-ui'), lease)
        probe = lambda: {'worker-ui'}
        self.assertEqual(addressed.listening(self.manifest, 'worker-ui', self.now, probe)[0], False)
        self.assertEqual(addressed.unconfirmed(self.manifest, 'worker-ui', lease, [], at=self.now), ['fresh'])
        self.assertTrue(addressed.listening(self.manifest, 'worker-ui', self.now + 1140, probe)[0])
        # Expiring delivery failures never makes a lapsed lease live by itself.
        self.assertFalse(addressed.listening(self.manifest, 'worker-ui', self.now + 1140, lambda: set())[0])

    def test_legacy_ids_expire_by_row_age_and_rearm_cannot_restart_the_timeout(self):
        self.lease('worker-ui', heartbeat_ago=38*3600)
        data = addressed.read_lease(self.manifest, 'worker-ui')
        data['unconfirmed'] = ['old']
        addressed.write_json(addressed.lease_path(self.manifest, 'worker-ui'), data)
        self.incumbent_row('old', 'x', 'worker-ui', ago=38*3600)
        self.assertEqual(addressed.unconfirmed(self.manifest, 'worker-ui', data, at=self.now), [])
        lease = self.adapter_lease('worker-ui')
        self.assertTrue(addressed.listening(self.manifest, 'worker-ui', self.now)[0])


    def test_recent_legacy_row_timestamp_expires_before_old_lease_start(self):
        self.manifest.addressed.confirm_timeout = '20m'
        self.lease('worker-ui', heartbeat_ago=2*3600)
        data = addressed.read_lease(self.manifest, 'worker-ui')
        data['unconfirmed'] = ['recent']
        addressed.write_json(addressed.lease_path(self.manifest, 'worker-ui'), data)
        self.incumbent_row('recent', 'x', 'worker-ui', ago=5*60)
        self.assertEqual(addressed.unconfirmed(self.manifest, 'worker-ui', data, at=self.now), ['recent'])
        self.assertFalse(addressed.listening(self.manifest, 'worker-ui', at=self.now)[0])
        self.assertEqual(addressed.unconfirmed(self.manifest, 'worker-ui', data, at=self.now + 16*60), [])


class HumanRecipientTests(AddressedCase):
    def test_question_human_actor_is_human_without_a_roster_entry(self):
        # The default operator and an explicit override carry the same human policy.
        for config, expected in (('', 'ck-sample-lead'),
                                 ('\n[questions]\nhuman_actor = "operator"\n', 'operator')):
            with self.subTest(actor=expected):
                self.file.write_text(BASE + ADDRESSED + config)
                manifest = load(self.file)
                self.assertEqual(manifest.questions.human_actor, expected)
                self.assertNotIn(expected, addressed.registry(manifest))
                self.assertTrue(addressed.is_human(manifest, expected))
                self.assertTrue(addressed.listening(manifest, expected)[0])
                self.assertFalse(addressed.is_human(manifest, 'worker-ui'))
                self.assertFalse(addressed.is_human(manifest, ''))

    def test_human_role_members_receive_only_the_hourly_digest(self):
        self.file.write_text(self.file.read_text().replace('coordinator = ["coordinator", "lead-a"]',
            'coordinator = { members = ["coordinator", "lead-a"], human = true }'))
        self.manifest = load(self.file)
        bus.configure(self.manifest)
        self.assertEqual(addressed.first_live(self.manifest, 'coordinator'), 'coordinator')
        for name in ('coordinator', 'lead-a'):
            self.assertTrue(addressed.listening(self.manifest, name)[0])
        self.quiet(addressed.send, self.manifest, 'x', 'coordinator', 'human role ask')
        self.quiet(addressed.send, self.manifest, 'x', 'lead-a', 'direct human ask')
        found, _ = self.sweep()
        self.assertFalse(found)
        first, _ = self.sweep(at=self.now + 901)
        self.assertFalse(first)
        self.assertFalse([r for r in self.rows() if r['id'].startswith(('wake-ob-', 'alert-ob-'))])
        self.assertIn('obligations: 2', self.digest_text())
        self.assertEqual(len(self.sweep(at=self.now + 5*3600)[0]), 0)
        self.assertEqual(len(self.sweep(at=self.now + 12*3600)[0]), 0)
        self.assertNotIn('ROLE_VACANT', '\n'.join(self.relay_lines()))
        self.assertNotIn('RECIPIENT_DEAF', '\n'.join(self.relay_lines()))

    def test_human_agent_without_a_role(self):
        self.file.write_text(self.file.read_text().replace('[addressed.agents.worker-ui]',
            '[addressed.agents.worker-ui]\nhuman = true'))
        self.manifest = load(self.file)
        self.assertTrue(addressed.listening(self.manifest, 'worker-ui')[0])
        self.assertEqual(self.quiet(addressed.send, self.manifest, 'x', 'worker-ui', 'ping')[0], 0)

    def test_human_flags_require_booleans(self):
        from coordkit.manifest import ManifestError
        for value in ('"true"', '1'):
            with self.subTest(value=value):
                self.file.write_text(BASE + ADDRESSED.replace(
                    '[addressed.agents.worker-ui]', '[addressed.agents.worker-ui]\nhuman = ' + value))
                with self.assertRaises(ManifestError):
                    load(self.file)


class BroadcastInboxTests(AddressedCase):
    def test_readers_own_reply_closes_a_broadcast_only_for_that_reader(self):
        self.incumbent_row('b1', 'lead-a', 'all', kind='msg', text='fleet notice')
        self.incumbent_row('r1', 'coordinator', 'relay', kind='fyi', text='done', re='b1', ago=30.0)
        self.assertEqual([r['id'] for r in addressed.inbox_rows(self.manifest, 'coordinator')], [])
        self.assertEqual([r['id'] for r in addressed.inbox_rows(self.manifest, 'relay')], ['b1'])
        self.assertEqual(addressed.inbox_rows(self.manifest, 'coordinator', all_rows=True)[0]['state'], 'done')


class BoundedUnackedTests(AddressedCase):
    def setUp(self):
        super().setUp()
        self.manifest.addressed.incumbent_waiters = True
        self.probe = patch.object(addressed, 'PROBE', lambda: set(addressed.registry(self.manifest)))
        self.probe.start()
        self.addCleanup(self.probe.stop)

    def test_one_wake_across_ticks_and_restart_then_hourly_digest(self):

        self.incumbent_row('bounded', 'x', 'worker-ui', ago=0)
        self.assertFalse(self.sweep(at=self.now+899)[0])
        self.assertEqual(len(self.sweep(at=self.now+901)[0]), 1)
        for hour in (1, 2, 3, 4, 5, 12):
            self.assertFalse(self.sweep(at=self.now+hour*3600)[0])
        self.assertEqual(len([r for r in self.rows() if r.get('re') == 'bounded']), 1)
        self.assertIn('bounded', self.digest_text())


    def test_unknown_recipient_and_owner_digest_only_and_self_lane_skipped(self):
        self.incumbent_row('unknown', 'x', 'legacy', ago=3600)
        self.incumbent_row('owner', 'x', 'worker-ui', owner='legacy-owner', ago=3600)
        self.incumbent_row('self', 'service-agent', 'worker-ui', ago=3600, priority='urgent')
        self.assertFalse(self.sweep()[0])
        digest = next(r for r in self.rows() if r.get('topic') == 'coordkit-digest')
        self.assertIn('obligations: 2', digest['text'])
        self.assertIn('ask unknown', digest['text'])
        self.assertIn('ask owner', digest['text'])
        self.assertNotIn('self', digest['text'])

    def test_legacy_hourly_keys_do_not_restart_an_alert_ladder(self):
        self.incumbent_row('legacy', 'x', 'worker-ui', ago=5*3600)
        addressed.write_json(addressed.home(self.manifest)/'sweep.json',
            {'baseline': self.now - 24*3600, 'seen': {'unacked:legacy:20': self.now - 3*3600}})
        self.assertFalse(self.sweep()[0])
        self.assertIn('obligations: 1', self.digest_text())


class DeafAlertBudgetTests(AddressedCase):
    def test_deaf_to_live_transition_cannot_add_a_second_wake(self):

        for name in ('coordinator', 'relay', 'lead-a'):
            self.lease(name, heartbeat_ago=-24*3600)
        self.incumbent_row('transition', 'x', 'worker-ui', ago=0)
        self.sweep()
        self.assertEqual([f['event'] for f in self.sweep(at=self.now+16*60)[0]], ['pickup'])
        self.lease('worker-ui', heartbeat_ago=-24*3600)
        for hour in (1, 4, 7):
            self.assertFalse(self.sweep(at=self.now+hour*3600)[0])
        self.assertEqual(len([r for r in self.rows() if r.get('re') == 'transition']), 1)
