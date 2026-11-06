"""Relay discovery is advisory routing evidence, never a local reader registry."""
import contextlib
import datetime as dt
import io
import json
import time
from unittest.mock import patch

from coordkit import bus, mcp_server, relay
from coordkit.manifest import Relay
from tests.support import FakeCase


class RelayDiscoveryTests(FakeCase):
    def setUp(self):
        super().setUp()
        self.manifest.bus.mirror = False
        self.manifest.relay = Relay(site='local')
        bus.configure(self.manifest)

    def seed(self, frm='remote-sender', to='remote-lane', site='peer-a', ago=0):
        row = dict(id=f'{frm}-{site}', ts=(dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=ago)).isoformat(),
                   frm=frm, to=to, kind='ask', text='work', origin=f'relay:{site}')
        path = self.manifest.paths.bus_log
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a') as stream:
            stream.write(json.dumps(row) + '\n')
        return row

    def post(self, to, kind='ask', *extra, code=0):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                result = bus.main(['post', '--from', 'local-sender', '--to', to, '--kind', kind, *extra, 'hello'], self.manifest)
            except SystemExit as exc:
                result = exc.code
        self.assertEqual(result or 0, code, out.getvalue() + err.getvalue())
        return out.getvalue() + err.getvalue()

    def test_only_relay_senders_are_known_across_sites_and_history(self):
        self.seed(ago=2*86400)
        self.seed(frm='other-sender', to='other-lane', site='peer-b')
        known = bus.known_recipients(bus.read())
        self.assertTrue({'remote-sender', 'other-sender'} <= known)
        self.assertTrue({'remote-lane', 'other-lane'}.isdisjoint(known))
        self.post('remote-lane', code=2)
        self.post('remote-sender')
        self.post('never-seen', code=2)
        self.assertFalse(any(r['to'] == 'never-seen' for r in bus.read()))

    def test_disabled_relay_does_not_learn_recipients_or_local_forced_targets(self):
        self.seed()
        self.manifest.relay = None
        self.post('remote-lane', code=2)
        self.post('forced-target', 'ask', '--force')
        self.post('forced-target', code=2)

    def test_addressed_owner_validation_accepts_relay_discovered_name(self):
        self.seed()
        self.manifest.addressed.agents = {'local-reader': {}}
        with patch('coordkit.addressed.after_post', return_value='delivered'):
            self.post('local-reader', 'ruling', '--owner', 'remote-sender')
            self.post('local-reader', 'ruling', '--owner', 'remote-lane', code=2)

    def test_agents_status_includes_asserted_senders_and_source_sites_only(self):
        self.seed(ago=2*86400)
        self.seed(site='peer-b')
        agents = {r['name']: r for r in mcp_server.read_call(self.manifest, 'agents_status', {})['agents']}
        self.assertEqual(agents['remote-sender']['sites'], ['peer-a', 'peer-b'])
        self.assertEqual(agents['remote-sender']['status'], 'remote')
        self.assertIsNone(agents['remote-sender']['listening'])
        self.assertIsNotNone(agents['remote-sender']['last_post_at'])
        self.assertNotIn('remote-lane', agents)
        self.assertNotIn('remote-lane', mcp_server.roster_names(self.manifest, bus.read(), time.time()))

    def test_local_roster_keeps_local_status_with_remote_source_shown(self):
        self.seed(frm='worker', to='lead')
        with patch('coordkit.addressed.listening', return_value=(False, 'no waiter lease')):
            agents = {r['name']: r for r in mcp_server.read_call(self.manifest, 'agents_status', {})['agents']}
        self.assertEqual(agents['worker']['status'], 'idle')
        self.assertEqual(agents['worker']['sites'], ['local', 'peer-a'])

    def test_remote_only_posts_never_probe_local_liveness_or_copy_even_after_local_post(self):
        self.seed(frm='remote-lane', to='all', ago=4*3600)
        self.manifest.bus.live_check = True
        with patch.object(bus, 'lane_liveness') as live, patch.object(bus, 'launched_at') as launch:
            for kind in ('ask', 'fyi', 'msg'):
                output = self.post('remote-lane', kind)
                self.assertNotIn('idle', output)
                self.assertNotIn('copying', output)
            live.assert_not_called()
            launch.assert_not_called()
        self.assertEqual(len(bus.read()), 4)
        self.assertFalse(any(r.get('copy_of') for r in bus.read()))

    def test_fyi_never_copies_or_warns_for_local_silent_or_dead_reader(self):
        self.manifest.bus.live_check = True
        with patch.object(bus, 'lane_liveness', return_value=(False, 'ended')) as live:
            output = self.post('worker', 'fyi')
            live.assert_not_called()
        self.assertNotIn('idle', output)
        self.assertEqual(len(bus.read()), 1)

    def test_local_reader_still_copies_and_remote_activity_does_not_mask_local_silence(self):
        self.seed(to='worker')
        self.seed(frm='worker', to='remote-lane')
        self.assertIn('lead copied', self.post('worker'))
        copies = [r for r in bus.read() if r.get('copy_of')]
        self.assertEqual(len(copies), 1)
        self.assertIn('silent', copies[0]['text'])

    def test_local_sender_evidence_restores_local_liveness_checks(self):
        self.seed()
        with self.manifest.paths.bus_log.open('a') as stream:
            stream.write(json.dumps(dict(id='local-old', ts='2000-01-01T00:00:00Z',
                                        frm='remote-lane', to='lead', kind='msg')) + '\n')
        self.assertIn('lead copied', self.post('remote-lane'))

    def test_sweep_does_not_rehome_remote_only_owner_as_deaf(self):
        from coordkit import addressed
        self.seed(ago=4*3600)
        self.seed(frm='remote-lane', to='all', ago=4*3600)
        with patch.object(addressed, 'listening', return_value=(False, 'no local reader')) as live, \
                patch.object(addressed, 'flush_lead_digest') as digest:
            addressed.sweep(self.manifest, out=lambda _: None)
        live.assert_not_called()
        self.assertTrue(digest.call_args.kwargs['pending'])
        self.assertEqual(digest.call_args.kwargs['dead'], [])
