"""Peer status derives solely from durable relay-origin bus rows."""
import contextlib
import datetime as dt
import io
import json
from unittest.mock import patch

from coordkit import lifecycle, relay
from coordkit.manifest import ManifestError, Relay
from coordkit.state import write_json
from tests.support import FakeCase


class RelayPeerTests(FakeCase):
    def setUp(self):
        super().setUp()
        self.at = 2_000_000_000
        self.manifest.relay = Relay(site='local')
        clock = patch('coordkit.relay.time.time', return_value=self.at)
        clock.start()
        self.addCleanup(clock.stop)
        self.connected()

    def connected(self, **extra):
        write_json(relay.directory(self.manifest)/'status.json',
                   dict(connected=True, at=self.at, last_frame=self.at, **extra))

    def add(self, site, age):
        path = self.manifest.paths.bus_log
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = dt.datetime.fromtimestamp(self.at - age, dt.timezone.utc).isoformat()
        with path.open('a') as stream:
            stream.write(json.dumps(dict(id=f'{site}-{age}', ts=ts, frm='sender', to='reader',
                                         kind='msg', origin=f'relay:{site}')) + '\n')
        return ts

    def test_status_groups_sites_counts_last_hour_and_uses_newest_source_timestamp(self):
        latest = self.add('peer-a', 10)
        self.add('peer-b', 30)
        self.add('peer-a', 3600)
        self.add('peer-a', 3601)
        self.add('local', 0)
        self.add('peer-future', -10)
        self.add('invalid site', 0)
        self.add('peer-ancient', 2*86400)
        # A local row or a partial inbound line cannot create a peer.
        with self.manifest.paths.bus_log.open('a') as stream:
            stream.write(json.dumps(dict(id='local', ts=latest, frm='local-writer', to='peer-name', kind='msg')) + '\n')
            stream.write('{"origin":"relay:partial"')
        peers = relay.status(self.manifest)['peers']
        self.assertEqual(set(peers), {'peer-a', 'peer-b', 'peer-ancient'})
        self.assertEqual(peers['peer-a'], dict(last_row_at=latest, last_row_age_seconds=10, rows_last_hour=2))
        self.assertEqual(peers['peer-b']['rows_last_hour'], 1)
        self.assertEqual(peers['peer-ancient']['rows_last_hour'], 0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            relay.main(['status'], self.manifest)
        self.assertEqual(json.loads(out.getvalue())['peers'], peers)

    def test_no_history_and_disabled_relay(self):
        self.assertEqual(relay.status(self.manifest)['peers'], {})
        self.assertFalse(relay.health(self.manifest).startswith('WARN'))
        self.add('peer', 7200)
        self.manifest.relay = None
        self.assertEqual(relay.status(self.manifest), {'enabled': False})
        self.assertEqual(relay.health(self.manifest), 'off')

    def test_warning_window_excludes_recent_and_old_peers_and_observes_exact_boundaries(self):
        for age, warns in ((3599, False), (3600, False), (3600.01, True), (86400, True), (86400.01, False)):
            with self.subTest(age=age):
                self.manifest.paths.bus_log.unlink(missing_ok=True)
                self.add('quiet-peer', age)
                result = relay.health(self.manifest)
                self.assertEqual(result.startswith('WARN silent_peers'), warns, result)

    def test_doctor_warns_for_silent_peer_without_hiding_other_relay_faults(self):
        self.add('quiet-peer', 7200)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            lifecycle.doctor(self.manifest)
        self.assertIn('DOCTOR relay warn WARN silent_peers', out.getvalue())
        self.connected(suppressed_rows=1)
        result = relay.health(self.manifest)
        self.assertTrue(result.startswith('WARN suppressed_rows'))
        self.assertIn('silent_peers', result)
        self.connected(error='relay_retention_gap')
        with self.assertRaisesRegex(ManifestError, 'relay_unhealthy'):
            relay.health(self.manifest)
        write_json(relay.directory(self.manifest)/'status.json',
                   dict(connected=True, at=self.at - 100, last_frame=self.at - 100))
        with self.assertRaisesRegex(ManifestError, 'relay_unhealthy'):
            relay.health(self.manifest)
