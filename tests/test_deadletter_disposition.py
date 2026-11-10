"""Audited dead-letter disposition: a named exit for one attributed letter."""
import contextlib
import io
import json
import unittest

from coordkit import deadletters, lifecycle
from tests.support import FakeCase

OURS = {'execution_id': 'exec-ours', 'phase': 'terminal', 'reason': 'anx_auth_required', 'created_at': '2000-01-01T00:00:00Z'}
OTHER = {'execution_id': 'exec-other', 'reason': 'card_unresolved', 'created_at': '2026-01-01T00:00:00Z'}


class DispositionTests(FakeCase):
    def setUp(self):
        super().setUp()
        req = self.manifest.paths.out_dir/'req'
        req.mkdir(parents=True, exist_ok=True)
        (req/'w.plan.json').write_text(json.dumps({'label': 'w', 'key': 'ck-sample-w'}))  # a lane this project launched

    def spool(self, *letters):
        self.respond({'tool': 'agentctl', 'contains': ['anx', 'status'], 'json': {'ok': True, 'result': {
                         'dead_letter_count': len(letters), 'dead_letters': list(letters)}}},
                     {'tool': 'agentctl', 'contains': ['status', 'exec-other'], 'json': {'ok': True, 'result': {'delegation': {'request_key': 'other-1'}}}},
                     {'tool': 'agentctl', 'contains': ['status', 'exec-ours'], 'json': {'ok': True, 'result': {'delegation': {'request_key': 'ck-sample-w'}}}})

    def doctor(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = lifecycle.main(list(args), self.manifest, 'doctor')
        return rc, out.getvalue()

    def test_disposition_clears_only_the_named_letter_with_a_reason_and_reopens(self):
        self.spool(OURS, OTHER)
        self.assertEqual([r['execution_id'] for r in deadletters.ours(self.manifest, [OURS, OTHER])], ['exec-ours'])
        with self.assertRaises(ValueError):  # a reason is required
            deadletters.disposition(self.manifest, 'exec-ours', 'ok', 'lead', [OURS, OTHER])
        with self.assertRaises(ValueError):  # another project's letter cannot be dispositioned here
            deadletters.disposition(self.manifest, 'exec-other', 'not ours but trying', 'lead', [OURS, OTHER])
        with self.assertRaises(ValueError):
            deadletters.disposition(self.manifest, 'exec-gone', 'not in the spool at all', 'lead', [OURS, OTHER])
        n = deadletters.disposition(self.manifest, 'exec-ours', 'anx auth not enrolled at pilot launch; run completed', 'lead', [OURS, OTHER])
        self.assertEqual(n, 1)
        self.assertEqual(deadletters.ours(self.manifest, [OURS, OTHER]), [])
        audit = json.loads((self.manifest.paths.root/'dead-letter-dispositions.json').read_text())
        self.assertEqual(next(iter(audit.values()))['reason'], 'anx auth not enrolled at pilot launch; run completed')
        # A changed letter for the same execution is visible again (keyed by content, not id).
        changed = dict(OURS, reason='http_500')
        self.assertEqual([r['reason'] for r in deadletters.ours(self.manifest, [changed])], ['http_500'])
        self.assertEqual(deadletters.reopen(self.manifest, 'exec-ours'), 1)
        self.assertEqual(len(deadletters.ours(self.manifest, [OURS])), 1)

    def test_unknown_ownership_cannot_be_dispositioned(self):
        unknown = {'execution_id': 'exec-unknown', 'reason': 'anx_auth_required', 'created_at': '2000-01-01T00:00:00Z'}
        self.respond({'tool': 'agentctl', 'contains': ['status', 'exec-unknown'], 'exit': 1, 'stdout': ''})
        self.assertEqual(len(deadletters.ours(self.manifest, [unknown])), 1, 'unknown counts as ours for doctor')
        with self.assertRaises(ValueError):
            deadletters.disposition(self.manifest, 'exec-unknown', 'anx auth not enrolled at launch', 'lead', [unknown])

    def test_doctor_cli_dispositions_and_reports_the_count(self):
        self.spool(OURS)
        rc, out = self.doctor('--disposition-dead-letter', 'exec-ours', '--reason', 'anx auth not enrolled at pilot launch')
        self.assertIn('DEAD_LETTER_DISPOSITIONED exec-ours entries=1', out)
        self.assertIn('dead_letters green', out)
        self.assertIn('dispositioned=1', out)

    def test_foreign_dead_letters_warn_without_becoming_this_projects_red_finding(self):
        self.spool(OTHER)
        rc, out = self.doctor()
        self.assertNotIn('DOCTOR dead_letters red', out)
        self.assertIn('DOCTOR dead_letters warn WARN host_dead_letters', out)
        self.assertIn('new=1 ours=0 foreign=1', out)

    def test_full_spool_warns_even_when_baselined_snapshot_has_no_new_rows(self):
        from coordkit.state import write_json
        baseline = {'count': 256, 'letters': [], 'fingerprints': []}
        write_json(self.manifest.paths.root/'dead-letter-baseline.json', baseline)
        with unittest.mock.patch.object(lifecycle, 'dead_letter_snapshot', return_value=baseline):
            rc, out = self.doctor()
        self.assertNotIn('DOCTOR dead_letters red', out)
        self.assertIn('DOCTOR dead_letters warn WARN host_dead_letters', out)
        self.assertIn('total=256 baseline=256 new=0 ours=0 foreign=0', out)

    def test_shadow_refuses_disposition(self):
        self.assertFalse(lifecycle.shadow_allowed('doctor', ['--disposition-dead-letter', 'x', '--reason', 'y']))
        self.assertTrue(lifecycle.shadow_allowed('doctor', []))

    def test_prefix_overlap_is_not_ownership(self):
        tools = {'execution_id': 'exec-tools', 'reason': 'anx_auth_required', 'created_at': '2000-01-01T00:00:00Z'}
        self.respond({'tool': 'agentctl', 'contains': ['status', 'exec-tools'], 'json': {'ok': True, 'result': {
            'delegation': {'request_key': 'ck-sample-tools-lane'}}}})  # another project whose prefix extends ours
        with self.assertRaises(ValueError):
            deadletters.disposition(self.manifest, 'exec-tools', 'not ours despite the prefix', 'lead', [tools])
        self.assertTrue(deadletters.issued(self.manifest, 'ck-sample-w-r1'))
        self.assertFalse(deadletters.issued(self.manifest, 'ck-sample-w2'))
