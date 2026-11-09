"""Unanswered coordination must converge without repeated wakes or ownerless work."""
import json
from unittest.mock import patch

from coordkit import addressed, approvals, bus, escalation, mcp_server, questions
from coordkit.manifest import ManifestError, load
from tests.test_addressed import AddressedCase


class ProcessDefaultsTests(AddressedCase):
    def setUp(self):
        super().setUp()
        self.manifest.bus.mirror = False
        self.lease('lead-a')

    def post(self, text='Use the cached result?', kind='ask', *flags):
        self.quiet(bus.main, ['post', '--from', 'worker-ui', '--to', 'lead-a',
                              '--kind', kind, '--topic', 'result', *flags, text], self.manifest)
        return bus.LAST_POST[0]

    def digest(self, at=None, state=None):
        self.quiet(addressed.flush_lead_digest, self.manifest, state if state is not None else {},
                   at or self.now, rows=self.rows())
        return self.digest_text()

    def test_repeat_normalizes_and_does_not_append_or_wake_even_with_force(self):
        with patch('coordkit.adapters.notify') as wake:
            original = self.post()
            self.post('  USE the cached RESULT?  ', 'ask', '--force')
            self.assertEqual(wake.call_count, 1)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(questions.load_all(self.manifest)[original['id']]['reasks'], 1)

    def test_second_reask_applies_default_receipt_and_closes(self):
        original = self.post('Use cache?', 'ask', '--default', 'Use cache')
        self.post('Use cache?', 'ask', '--default', 'Use cache')
        self.assertEqual(len(self.rows()), 1)
        self.post('Use cache?', 'ask', '--default', 'Use cache')
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[1]['kind'], rows[1]['re']), ('answer', original['id']))
        self.assertEqual(rows[1]['text'], 'auto-default applied after 2 unanswered re-asks: Use cache')
        self.assertEqual(questions.open_questions(self.manifest), [])
        self.post('Use cache?', 'ask', '--default', 'Use cache')
        self.assertEqual(len(self.rows()), 2)

    def test_long_default_uses_the_existing_bus_ref_contract(self):
        default = 'cached result ' * 100
        for _ in range(3):
            self.post('Use cache?', 'ask', '--default', default)
        row = self.rows()[-1]
        self.assertLessEqual(len(row['text']), self.manifest.bus.text_cap)
        from coordkit.refs import ref_file
        self.assertIn(default, ref_file(self.manifest, row, full=True).read_text())

    def test_default_change_supersedes_and_answered_ask_can_be_asked_again(self):
        first = self.post('Use cache?', 'ask', '--key', 'cache', '--default', 'Use cache')
        second = self.post('Use cache?', 'ask', '--key', 'cache', '--default', 'Fetch fresh')
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(questions.load_all(self.manifest)[first['id']]['state'], 'closed')
        self.incumbent_row('reply', 'lead-a', 'worker-ui', 'answer', 'Fetch fresh', ago=0, re=second['id'])
        third = self.post('Use cache?', 'ask', '--key', 'cache', '--default', 'Fetch fresh')
        self.assertNotEqual(second['id'], third['id'])

    def test_default_receipt_recovers_after_question_state_write_failure(self):
        self.post('Use cache?', 'ask', '--default', 'Use cache')
        self.post('Use cache?', 'ask', '--default', 'Use cache')
        with patch.object(questions, 'write_json', side_effect=OSError('interrupted projection')):
            with self.assertRaises(OSError):
                self.post('Use cache?', 'ask', '--default', 'Use cache')
        self.assertEqual(len(self.rows()), 2)  # answer is durable before closure
        self.post('Use cache?', 'ask', '--default', 'Use cache')
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(questions.open_questions(self.manifest), [])

    def test_direct_escalation_repeats_share_one_receipt_and_counter(self):
        self.lease('relay')
        source = dict(id='source', frm='worker-ui', to='other', topic='result', kind='ask')
        with patch.object(addressed, 'digest_only', return_value=False), patch('coordkit.adapters.notify') as wake:
            for _ in range(3):
                addressed.escalate(self.manifest, 'recipient_deaf', 'The result needs an answer', source,
                                   fallback='relay', at=self.now)
            self.assertEqual(wake.call_count, 1)
        records = questions.load_all(self.manifest)
        self.assertEqual(len(records), 1)
        self.assertEqual(next(iter(records.values()))['reasks'], 2)

    def test_no_default_is_digested_once_and_never_rewakes(self):
        original = self.post()
        for _ in range(4):
            self.post()
        self.assertEqual(len(self.rows()), 1)
        state = {}
        self.digest(state=state)
        self.digest(at=self.now + 3601, state=state)
        self.assertEqual(self.digest_text().count('unanswered, no default'), 1)
        self.assertIn('open questions: 1; oldest ' + original['id'], self.digest_text())
        self.assertTrue(questions.load_all(self.manifest)[original['id']]['no_default_digested'])
        findings, _ = self.sweep(at=self.now + 7202)
        self.assertFalse(any(f.get('item', {}).get('id') == original['id'] for f in findings))

    def test_auto_default_opt_out_still_deduplicates(self):
        original = self.post('Use cache?', 'ask', '--default', 'Use cache', '--no-auto-default')
        for _ in range(3):
            self.post('Use cache?', 'ask', '--default', 'Use cache', '--no-auto-default')
        self.quiet(questions.reconcile_defaults, self.manifest, self.rows(), self.now + 7200)
        record = questions.load_all(self.manifest)[original['id']]
        self.assertEqual((record['state'], record['reasks']), ('open', 3))
        self.assertNotIn('unanswered_no_default', record)
        self.assertEqual(len(self.rows()), 1)

    def test_age_limit_applies_without_reask(self):
        self.manifest.questions.auto_default_after = '10m'
        original = self.post('Use cache?', 'ask', '--default', 'Use cache')
        questions.reconcile_defaults(self.manifest, self.rows(), self.now + 601)
        self.assertEqual(questions.load_all(self.manifest)[original['id']]['state'], 'closed')
        self.assertIn('after 0 unanswered re-asks', self.rows()[-1]['text'])

    def test_real_answer_wins_and_an_explicit_key_allows_new_information(self):
        original = self.post('Use cache?', 'ask', '--key', 'cache', '--default', 'Use cache')
        self.incumbent_row('reply', 'lead-a', 'worker-ui', 'answer', 'Use fresh data', ago=0, re=original['id'])
        questions.reconcile_defaults(self.manifest, self.rows(), self.now + 7200)
        self.assertFalse(any(r.get('auto_default') for r in self.rows()))
        self.post('Cache is stale. Use a fresh result?', 'ask', '--key', 'cache', '--default', 'Fetch fresh')
        self.post('Cache is stale. Use a fresh result?', 'ask', '--key', 'cache', '--default', 'Fetch fresh')
        records = questions.load_all(self.manifest)
        self.assertEqual(len(records), 2)
        self.assertEqual([r['reasks'] for r in records.values() if r['state'] == 'open'], [1])

    def test_owner_missing_after_thirty_minutes_and_claim_clears(self):
        approval = self.post('Approved: run the work', 'ruling')
        self.assertTrue(approval['owner_missing'])
        rows = self.rows()
        self.assertEqual(approvals.findings(self.manifest, rows, self.now + 1799), [])
        finding = approvals.findings(self.manifest, rows, self.now + 1801)[0]
        self.assertIn('topic=result approval=' + approval['id'], finding['text'])
        self.digest(at=self.now + 1801)
        self.assertIn('owner-missing topic=result', self.digest_text())
        self.post('I own it', 'claim')
        self.assertEqual(approvals.findings(self.manifest, self.rows(), self.now + 3601), [])
        result = mcp_server.topic_rows(self.manifest, self.rows(), {}, {}, self.now + 3601)
        item = next(i for i in result['topics'] if i['topic'] == 'result')
        self.assertEqual((item['owner'], item['owner_missing']), ('worker-ui', False))

    def test_later_owner_on_a_post_clears_and_approval_opt_out(self):
        self.post('Approved', 'approval')
        self.post('Assigned', 'msg', '--owner', 'worker-ui')
        self.assertEqual(approvals.findings(self.manifest, self.rows(), self.now + 3601), [])
        self.post('Approved without an owner by exception', 'ruling', '--no-owner-required')
        self.assertEqual(approvals.findings(self.manifest, self.rows(), self.now + 3601), [])

    def test_owner_opt_out_without_prior_owner(self):
        row = self.post('Approved by exception', 'approval', '--no-owner-required')
        self.assertNotIn('owner_missing', row)
        self.assertEqual(approvals.findings(self.manifest, self.rows(), self.now + 3601), [])

    def test_owner_grace_is_configurable_and_claim_can_reference_approval(self):
        self.manifest.obligations.owner_required_after = '5m'
        approval = self.post('Approved', 'approval')
        self.assertEqual(len(approvals.findings(self.manifest, self.rows(), self.now + 301)), 1)
        self.quiet(bus.main, ['post', '--from', 'worker-ui', '--to', 'lead-a', '--kind', 'claim',
                              '--re', approval['id'], 'I own it'], self.manifest)
        self.assertEqual(self.rows()[-1]['topic'], 'result')
        self.assertEqual(approvals.findings(self.manifest, self.rows(), self.now + 301), [])

    def test_malformed_rows_and_queue_fail_open(self):
        self.assertEqual(approvals.findings(self.manifest, [None, {'topic': [], 'ts': []},
                         {'id': 'bad-ts', 'topic': 'result', 'ts': [], 'kind': 'ruling'},
                         {'id': 'bad', 'topic': 'result', 'ts': 'bad', 'kind': 'ruling'}], self.now), [])
        path = addressed.home(self.manifest)/'lead-digest.ndjson'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{broken\n[]\n' + json.dumps({'text': 5}) + '\n')
        addressed.to_lead_digest(self.manifest, 'finding', 'valid')
        self.digest()
        self.assertIn('valid', self.digest_text())

    def test_digest_escalations_dedupe_and_auto_default_without_wake(self):
        with patch.object(addressed, 'digest_only', return_value=True), patch('coordkit.adapters.notify'):
            for _ in range(3):
                escalation.ask(self.manifest, 'card:task', situation='Choose cached results',
                               impact='The result is needed', recommend='Use cache', why='It is still valid',
                               default='Use cache', key='cache')
        self.assertEqual(len([r for r in self.rows() if r['kind'] == 'ask']), 1)
        self.assertEqual(len([r for r in self.rows() if r.get('auto_default')]), 1)
        self.assertTrue(self.rows()[0]['digest_only'])

    def test_mcp_optional_fields_and_question_projection(self):
        self.manifest.mcp.identity = 'worker-ui'
        result = mcp_server.write_call(self.manifest, 'post', dict(to='lead-a', kind='ask', text='Use cache?',
                                      topic='result', key='cache', default='Use cache', no_auto_default=True))
        self.assertTrue(result['ok'])
        result = mcp_server.write_call(self.manifest, 'post', dict(to='lead-a', kind='ask', text='Use cache?',
                                      topic='result', key='cache', default='Use cache', no_auto_default=True))
        self.assertTrue(result['ok'])
        result = mcp_server.question_rows(self.manifest, {}, {}, self.now)
        self.assertEqual(result['questions'][0]['default'], 'Use cache')
        self.assertEqual(result['questions'][0]['reasks'], 1)
        mcp_server.write_call(self.manifest, 'post', dict(to='lead-a', kind='approval', text='Approved',
                             topic='other', no_owner_required=True))
        self.assertEqual(approvals.findings(self.manifest, self.rows(), self.now + 3601), [])

    def test_capture_reask_limit_and_sweep_observation_is_not_a_reask(self):
        with patch.object(questions, 'route') as route:
            record, _ = questions.create(self.manifest, 'worker-ui', 'Use cache?', default='Use cache')
            questions.create(self.manifest, 'worker-ui', 'Use cache?', default='Use cache', capture='sweep')
            questions.create(self.manifest, 'worker-ui', 'Use cache?', default='Use cache')
            questions.create(self.manifest, 'worker-ui', 'Use cache?', default='Use cache')
        self.assertEqual(route.call_count, 1)
        stored = questions.load_all(self.manifest)[record['qid']]
        self.assertEqual(stored['state'], 'answered')
        self.assertTrue(stored['auto_default_applied'])
        decision = next(r for r in self.rows() if r.get('id') == stored['answer_id'])
        self.assertEqual((decision['kind'], decision['rollback']), ('decision', questions.DEFAULT_ROLLBACK))

    def test_manifest_limits_are_configurable_and_positive(self):
        self.file.write_text(self.file.read_text() + '\n[questions]\nauto_default_reasks = 3\nauto_default_after = "2h"\n')
        manifest = load(self.file)
        self.assertEqual(manifest.questions.auto_default_reasks, 3)
        self.file.write_text(self.file.read_text().replace('auto_default_reasks = 3', 'auto_default_reasks = 0'))
        with self.assertRaises(ManifestError):
            load(self.file)
