"""Local listening evidence must not create work on another site."""
import contextlib
import io
import json
import time
from unittest.mock import patch

from coordkit import addressed, bus, relay
from coordkit.manifest import Relay
from tests.support import FakeCase


class RelayLocalTests(FakeCase):
    def setUp(self):
        super().setUp()
        self.manifest.bus.mirror = False
        self.manifest.relay = Relay(site='local')
        bus.configure(self.manifest)
        self.manifest.paths.bus_log.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.paths.bus_log.touch()
        self.store = relay.Store(self.manifest)
        self.addCleanup(self.store.close)
        self.store.scan()

    def post(self, to, text='work'):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            bus.main(['post', '--from', 'sender', '--to', to, '--kind', 'ask', text], self.manifest)

    def assert_local_delivery_only(self, row, notify):
        self.assertIs(row['site_local'], True)
        self.assertTrue(bus.for_lane(row, row['to']))
        self.assertTrue(any(row['id'] == r['id'] for call in notify.call_args_list for r in call.args[1]))
        # The append sink must skip it immediately, before any catch-up scan.
        queued = [json.loads(r[0])['id'] for r in self.store.db.execute('SELECT row FROM outbox')]
        self.assertNotIn(row['id'], queued)
        self.store.scan()
        self.assertNotIn(row['id'], [frame['row']['id'] for frame in self.store.pending()])

    def test_idle_copy_delivers_locally_but_never_enters_outbox_or_blocks_later_rows(self):
        with patch('coordkit.wake.notify') as notify:
            self.post('worker')
        original, copy = bus.read()
        self.assertEqual(copy['copy_of'], original['id'])
        self.assertEqual(copy['to'], 'lead')
        self.assert_local_delivery_only(copy, notify)
        self.assertEqual([f['row']['id'] for f in self.store.pending()], [original['id']])
        self.post('lead', 'later work')
        self.store.scan()
        self.assertEqual([f['seq'] for f in self.store.pending()], [0, 2])

    def test_addressed_fallback_escalation_keeps_local_delivery_and_skips_outbox(self):
        self.manifest.addressed.agents = {'worker': {'fallback': 'backup'}, 'backup': {}}
        with patch.object(addressed, 'listening', side_effect=lambda m, name, *a, **kw: (name == 'backup', 'test')), \
                patch('coordkit.wake.notify') as notify:
            self.post('worker')
        original, escalation = bus.read()
        self.assertEqual(escalation['escalation'], 'recipient_deaf')
        self.assertEqual(escalation['to'], 'backup')
        self.assertEqual(escalation['re'], original['id'])
        self.assert_local_delivery_only(escalation, notify)
        self.assertEqual([f['row']['id'] for f in self.store.pending()], [original['id']])

    def test_liveness_digest_delivers_locally_and_scanner_skips_unsunk_local_rows(self):
        addressed.to_lead_digest(self.manifest, 'recipient_deaf', 'Local reader is deaf')
        with patch('coordkit.wake.notify') as notify, contextlib.redirect_stdout(io.StringIO()):
            addressed.flush_lead_digest(self.manifest, {}, time.time(), out=lambda _: None)
        digest = bus.read()[0]
        self.assertEqual(digest['to'], 'lead')
        self.assert_local_delivery_only(digest, notify)
        # Recovery for an older writer that bypasses the append sink uses the same policy.
        with self.manifest.paths.bus_log.open('a') as stream:
            stream.write(json.dumps(dict(digest, id='unsunk-local')) + '\n')
        self.store.scan()
        self.assertEqual(self.store.get('scan')['count'], 2)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM outbox').fetchone()[0], 0)
