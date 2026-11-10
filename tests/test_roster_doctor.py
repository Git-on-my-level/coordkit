"""Doctor roster drift: bounded counts, warnings, and configurable thresholds."""
import contextlib
import io
import json
from unittest.mock import patch

from coordkit import roster
from coordkit.manifest import load
from tests.support import FakeCase
from tests.test_addressed import AddressedCase


class RosterDoctorTests(AddressedCase):
    def doctor_line(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            from coordkit import lifecycle
            lifecycle.doctor(self.manifest)
        return next(line for line in out.getvalue().splitlines() if line.startswith('DOCTOR roster '))

    def test_silent_enrolled_agent_warns_without_listening_evidence(self):
        self.incumbent_row('recent', 'lead-a', 'all', ago=60)
        line = self.doctor_line()
        self.assertIn('DOCTOR roster warn WARN', line)
        self.assertIn('silent=3', line)
        self.assertIn('silent_sample=coordinator,relay,worker-ui', line)

    def test_unenrolled_poster_warns_but_alias_and_declared_reader_do_not(self):
        for index in range(21):
            self.incumbent_row(f'rogue-{index}', 'rogue', 'all', ago=60)
            self.incumbent_row(f'alias-{index}', 'alias-name', 'all', ago=60)
            self.incumbent_row(f'reader-{index}', 'declared-reader', 'all', ago=60)
        parent = self.manifest.paths.bus_log.parent
        (parent / 'aliases.json').write_text(json.dumps({'aliases': {'alias-name': 'canonical'}}))
        (parent / 'readers.json').write_text(json.dumps({'readers': {'declared-reader': {'every_minutes': 5}}}))
        line = self.doctor_line()
        self.assertIn('unenrolled=1', line)
        self.assertIn('unenrolled_sample=rogue', line)
        self.assertNotIn('alias-name', line)
        self.assertNotIn('declared-reader', line)

    def test_internal_sender_is_not_an_unenrolled_agent(self):
        for index in range(21):
            self.incumbent_row(f'ck-{index}', 'coordkit', 'all', ago=60)
            self.incumbent_row(f'ck-report-{index}', 'coordkit-report', 'all', ago=60)
        line = self.doctor_line()
        self.assertIn('unenrolled=0', line)
        self.assertNotIn('unenrolled_sample=coordkit', line)

    def test_enrolled_sink_warns_when_it_receives_and_sends_nothing(self):
        for index in range(21):
            self.incumbent_row(f'mail-{index}', 'lead-a', 'worker-ui', ago=60)
        line = self.doctor_line()
        self.assertIn('sink=1', line)
        self.assertIn('sink_sample=worker-ui', line)

    def test_thresholds_are_configurable(self):
        self.manifest.addressed.roster_silent_after = '1h'
        self.manifest.addressed.roster_unenrolled_posts = 2
        self.manifest.addressed.roster_sink_received = 2
        self.incumbent_row('quiet', 'coordinator', 'all', ago=7200)
        for index in range(3):
            self.incumbent_row(f'rogue-{index}', 'rogue', 'all', ago=60)
            self.incumbent_row(f'mail-{index}', 'lead-a', 'worker-ui', ago=60)
        line = self.doctor_line()
        self.assertIn('silent=3', line)
        self.assertIn('unenrolled=1', line)
        self.assertIn('sink=1', line)

    def test_truncated_tail_does_not_invent_silence(self):
        self.incumbent_row('old', 'worker-ui', 'all', ago=100 * 3600)
        for index in range(20):
            self.incumbent_row(f'recent-{index}', 'lead-a', 'all', ago=60)
        with patch.object(roster, 'TAIL_BYTES', 200):
            line = self.doctor_line()
        self.assertIn('truncated=true', line)
        self.assertIn('silent=0', line)


class RosterManifestTests(FakeCase):
    def test_roster_thresholds_load_from_addressed_table(self):
        self.file.write_text(self.file.read_text() + '''
[addressed]
roster_silent_after = "1h"
roster_unenrolled_posts = 3
roster_sink_received = 4
''')
        manifest = load(self.file)
        self.assertEqual(manifest.addressed.roster_silent_after, '1h')
        self.assertEqual(manifest.addressed.roster_unenrolled_posts, 3)
        self.assertEqual(manifest.addressed.roster_sink_received, 4)
