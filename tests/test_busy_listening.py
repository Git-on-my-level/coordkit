"""A busy bound session is listening even while no bus waiter is armed."""
import os
from dataclasses import replace
from unittest.mock import patch

from coordkit import addressed, deliver, questions
from coordkit.manifest import ManifestError, load
from coordkit.state import write_json
from tests.test_addressed import AddressedCase


class BusyListeningTests(AddressedCase):
    def bound_transcript(self, age=42, session='session1', name='worker-ui'):
        config = self.root/'claude'
        path = config/'projects'/'-work'/f'{session}.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}\n')
        os.utime(path, (self.now - age, self.now - age))
        self.manifest = replace(self.manifest, questions=replace(self.manifest.questions, config_dirs=[str(config)]))
        write_json(questions.home(self.manifest)/'sessions.json', {session: {'name': name}})
        return path

    def test_bound_session_transcript_without_lease_or_hook(self):
        self.bound_transcript()
        self.assertEqual(addressed.listening(self.manifest, 'worker-ui', at=self.now),
                         (True, 'busy: transcript grew 42s ago'))
        self.assertEqual(addressed.listening(self.manifest, 'unknown', at=self.now), (False, 'no waiter lease'))
        self.lease('worker-ui', heartbeat_ago=400)
        self.assertTrue(addressed.listening(self.manifest, 'worker-ui', at=self.now)[0])

    def test_expired_and_unbound_transcripts_preserve_behavior(self):
        path = self.bound_transcript(age=601)
        self.assertFalse(addressed.listening(self.manifest, 'worker-ui', at=self.now)[0])
        path.touch()
        write_json(questions.home(self.manifest)/'sessions.json', {'session1': {'name': 'other'}})
        self.assertFalse(addressed.listening(self.manifest, 'worker-ui', at=self.now)[0])
        path.unlink()
        self.assertFalse(addressed.listening(self.manifest, 'other', at=self.now)[0])

    def test_hook_window_and_busy_transcript_continue_after_hook_expires(self):
        path = self.bound_transcript(age=42)
        note = dict(name='worker-ui', at=self.now - 30, session_id='session1', transcript_path=str(path), via='hook')
        write_json(deliver.note_path(self.manifest, 'worker-ui'), note)
        self.assertEqual(addressed.listening(self.manifest, 'worker-ui', at=self.now), (True, 'hook delivery 30s ago'))
        self.assertIn('busy: transcript grew', addressed.listening(self.manifest, 'worker-ui', at=self.now + 100)[1])
        self.assertFalse(addressed.listening(self.manifest, 'worker-ui', at=self.now + 601)[0])

    def test_hook_records_transcript_binding_without_question_bind(self):
        path = self.root/'env-bound.jsonl'
        path.write_text('{}\n')
        os.utime(path, (self.now - 42, self.now - 42))
        write_json(deliver.note_path(self.manifest, 'worker-ui'),
                   dict(name='worker-ui', at=self.now - 150, transcript_path=str(path), session_id='env-session'))
        self.assertEqual(addressed.listening(self.manifest, 'worker-ui', at=self.now),
                         (True, 'busy: transcript grew 42s ago'))
        write_json(questions.home(self.manifest)/'sessions.json', {'env-session': {'name': 'other'}})
        self.assertFalse(addressed.listening(self.manifest, 'worker-ui', at=self.now)[0])

    def test_live_waiter_keeps_existing_evidence(self):
        self.lease('worker-ui')
        self.bound_transcript()
        self.assertEqual(addressed.listening(self.manifest, 'worker-ui', at=self.now), (True, 'lease renewed 0s ago'))

    def test_explicit_hook_delivery_can_override_recent_unconfirmed_sidecar(self):
        self.lease('worker-ui')
        lease = addressed.read_lease(self.manifest, 'worker-ui')
        lease['unconfirmed'] = ['old']
        lease['unconfirmed_at'] = {'old': self.now}
        write_json(addressed.lease_path(self.manifest, 'worker-ui'), lease)
        self.assertFalse(addressed.listening(self.manifest, 'worker-ui', at=self.now, rows=[])[0])
        write_json(deliver.note_path(self.manifest, 'worker-ui'), dict(name='worker-ui', at=self.now - 30))
        self.assertEqual(addressed.listening(self.manifest, 'worker-ui', at=self.now, rows=[]), (True, 'hook delivery 30s ago'))

    def test_busy_window_default_override_and_validation(self):
        self.assertEqual(self.manifest.addressed.busy_window, '10m')
        self.bound_transcript(age=120)
        self.manifest = replace(self.manifest, addressed=replace(self.manifest.addressed, busy_window='1m'))
        self.assertFalse(addressed.listening(self.manifest, 'worker-ui', at=self.now)[0])
        self.file.write_text(self.file.read_text().replace('[addressed]\n', '[addressed]\nbusy_window = "2m"\n'))
        self.assertEqual(load(self.file).addressed.busy_window, '2m')
        self.file.write_text(self.file.read_text().replace('busy_window = "2m"', 'busy_window = "bad"'))
        with self.assertRaisesRegex(ManifestError, 'busy_window'):
            load(self.file)

    def test_bad_optional_evidence_and_future_timestamps_do_not_break_lease(self):
        note = deliver.note_path(self.manifest, 'worker-ui')
        write_json(note, dict(name='worker-ui', at=self.now + 10))
        self.assertFalse(addressed.listening(self.manifest, 'worker-ui', at=self.now)[0])
        note.write_text('{broken')
        self.assertFalse(addressed.listening(self.manifest, 'worker-ui', at=self.now)[0])
        self.lease('worker-ui', heartbeat_ago=300)
        self.assertIn('lease lapsed', addressed.listening(self.manifest, 'worker-ui', at=self.now)[1])
