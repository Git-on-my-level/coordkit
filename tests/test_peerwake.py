"""A held interactive lane is woken through a resident helper in its own profile."""
import json
import time
from unittest.mock import patch

from coordkit import peerwake, wake
from coordkit.manifest import load
from coordkit.state import write_json
from tests.support import FakeCase

LANES = '''
[[lanes]]
name = "deploy"
config_dir = "/profiles/.claude-1"
cwd = "."
model = "configured"
effort = "configured"
session_id = "sid-deploy"

[[lanes]]
name = "pipeline"
config_dir = "/profiles/.claude-1"
cwd = "."
model = "configured"
effort = "configured"
'''


def session(pid, sid, profile='.claude-1', tmux=None, status='idle', name='omi-75'):
    return dict(profile=profile, pid=pid, session_id=sid, name=name, name_source='derived',
                tmux=tmux, status=status, kind='interactive', cwd='.', socket=None)


class PeerWakeTests(FakeCase):
    def setUp(self):
        super().setUp()
        self.file.write_text(self.file.read_text() + LANES)
        self.manifest = load(self.file)
        self.sessions = [session(28548, 'sid-deploy', tmux='cl2-deploy:@31.%31'),
                         session(69574, 'sid-other', profile='.claude'),
                         session(31313, 'sid-pipe', profile='.claude-2', tmux='pipeline:@34.%34')]
        self.adapter = peerwake.ClaudePeer(discover=lambda: self.sessions, is_alive=lambda pid: True)
        self.rows = [dict(id='a1', to='deploy', frm='lead', kind='ask')]

    def helper(self, profile='.claude-1', ago=0):
        write_json(peerwake.heartbeat_path(self.manifest, profile), {'pid': 999, 'at': time.time() - ago})

    def test_pinned_session_resolves_across_profiles_not_by_shared_name(self):
        row, error = peerwake.target(self.manifest, 'deploy', self.sessions)
        self.assertEqual((row['profile'], row['pid'], error), ('.claude-1', 28548, None))
        self.assertEqual(peerwake.target(self.manifest, 'pipeline', self.sessions)[0]['pid'], 31313)
        self.assertEqual(peerwake.target(self.manifest, 'omi-75', self.sessions), (None, 'not_a_manifest_lane: omi-75'))

    def test_no_helper_means_unavailable_and_no_request(self):
        result = self.adapter.wake(self.manifest, 'deploy', self.rows, 'k1')
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['reason'], 'no_helper: .claude-1')
        self.assertFalse((peerwake.root(self.manifest, '.claude-1')/'pending').exists())
        self.helper(ago=peerwake.HELPER_FRESH_SECONDS + 5)
        self.assertEqual(self.adapter.wake(self.manifest, 'deploy', self.rows, 'k1')['status'], 'unavailable')

    def test_busy_session_is_listening(self):
        self.sessions[0]['status'] = 'busy'
        self.assertTrue(self.adapter.listening(self.manifest, 'deploy', self.rows))
        self.assertEqual(self.adapter.wake(self.manifest, 'deploy', self.rows, 'k1')['status'], 'listening')

    def test_request_claim_ack_and_read_receipt(self):
        self.helper()
        result = self.adapter.wake(self.manifest, 'deploy', self.rows, 'k1')
        self.assertEqual((result['status'], result['session']), ('requested', '.claude-1:28548'))
        self.assertEqual(self.adapter.wake(self.manifest, 'deploy', self.rows, 'k1')['status'], 'requested')
        claimed = peerwake.claim(self.manifest, '.claude-1')
        self.assertEqual([r['key'] for r in claimed], ['k1'])
        self.assertIn('a1', claimed[0]['text'])
        self.assertIn('never with &', claimed[0]['text'])
        self.assertEqual(peerwake.claim(self.manifest, '.claude-1'), [])
        record = peerwake.ack(self.manifest, '.claude-1', 'k1', 'sent')
        self.assertEqual(peerwake.read_receipt(record, self.sessions), 'sent')
        self.sessions[0]['status'] = 'busy'
        self.assertEqual(peerwake.read_receipt(record, self.sessions), 'read')
        results = (peerwake.root(self.manifest, '.claude-1')/'results.jsonl').read_text().splitlines()
        self.assertEqual(json.loads(results[0])['result'], 'sent')
        with self.assertRaises(FileNotFoundError):
            peerwake.ack(self.manifest, '.claude-1', 'k1', 'sent')

    def test_notify_records_the_peer_request_for_a_held_lane(self):
        self.helper()
        with patch('coordkit.wake.wake_adapters', return_value=[self.adapter]):
            wake.notify(self.manifest, self.rows)
        record = wake.wake_record(self.manifest, 'deploy')
        self.assertEqual(record['status'], 'requested')
        self.assertEqual(record['attempts'][0]['adapter'], 'claude-peer')
        self.assertEqual(len(peerwake.claim(self.manifest, '.claude-1')), 1)

    def test_helper_refuses_outside_a_session_of_its_profile(self):
        with patch.object(peerwake.sessions, 'discover', return_value=[]):
            self.assertIsNone(peerwake.owner_session('.claude-1'))
