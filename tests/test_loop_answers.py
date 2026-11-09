"""Human replies remain correlated after recovery and across both ANX consumers."""
import contextlib
import io
from unittest.mock import patch

from coordkit import bus, loop, loop_answers, questions, reconcile, wait
from coordkit.anxcli import AnxClient
from coordkit.process import ToolError
from coordkit.state import read_json, write_json
from tests.support import FakeCase


class LoopAnswersTests(FakeCase):
    def setUp(self):
        super().setUp()
        self.state = self.manifest.paths.root/'loops/audit'
        self.state.mkdir(parents=True)
        self.manifest.loop.fail_report_after = 1

    def quiet(self, fn, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return fn(*args, **kwargs)

    def record_failure(self):
        self.quiet(loop.record_failure, self.manifest, 'audit', self.manifest.loop, 'loop-audit', self.state, 'runner failed')
        return read_json(self.state/'escalation.json')

    def rows(self):
        bus.configure(self.manifest)
        return bus.read()

    def answers(self, ask_id):
        return {'tool': 'anx', 'contains': ['await'], 'json': {'ok': True, 'result': {'answers': [
            {'ask_id': ask_id, 'subject_ref': 'card:loop-audit', 'answer': {'text': 'Pause until reviewed'}}]}}}

    def test_answer_after_recovery_routes_once_without_ambiguity_or_automatic_pause(self):
        report = self.record_failure()
        ask_id = report['ask_id']
        self.assertEqual(self.quiet(loop.run_cycle, self.manifest, 'audit'), 0)
        self.assertFalse((self.state/'escalation.json').exists())
        self.assertTrue(loop_answers.path(self.manifest, ask_id).exists())
        self.manifest.escalation.internal_to = 'worker'
        self.respond(self.answers(ask_id))
        for at in (10000, 20000):
            self.quiet(reconcile.reconcile, self.manifest, at=at)
        rows = [row for row in self.rows() if row['id'] == 'answer-' + ask_id]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['kind'], 'decision')
        self.assertEqual(rows[0]['to'], 'worker')
        self.assertEqual(rows[0]['subject'], 'card:loop-audit')
        self.assertEqual(rows[0]['text'], 'Pause until reviewed')
        self.assertEqual(read_json(self.manifest.paths.root/'reconcile/answers.json'), [])
        self.assertFalse(self.escalations(), 'a recognized loop reply must not become an ambiguous-answer finding')
        self.assertFalse((self.state/'STOP').exists())
        self.assertFalse(any('resolve' in call['args'] for call in self.calls()))

    def test_question_consumer_routes_loop_answer_and_preserves_other_answers(self):
        ask_id = self.record_failure()['ask_id']
        response = self.answers(ask_id)
        response['json']['result']['answers'].append({'ask_id': 'event:other', 'answer': {'text': 'Other answer'}})
        self.respond(response)
        remaining = self.quiet(questions.anx_answers, self.manifest)
        self.assertEqual(remaining['event:other'], {'text': 'Other answer'})
        self.assertEqual(remaining[ask_id], {'text': 'Pause until reviewed'})
        self.assertEqual(self.rows()[0]['id'], 'answer-' + ask_id)

    def test_late_answer_to_superseded_ask_is_delivered_once_and_new_id_still_routes(self):
        with patch.object(loop.time, 'time', return_value=1000):
            first = self.record_failure()['ask_id']
        with patch.object(loop.time, 'time', return_value=4600):
            second = self.record_failure()['ask_id']
        self.assertNotEqual(first, second)
        args = [c['args'] for c in self.calls() if c['tool'] == 'anx' and 'ask' in c['args']][-1]
        self.assertEqual(args[args.index('--supersedes') + 1], first)
        self.assertTrue(loop_answers.path(self.manifest, first).exists())
        self.assertTrue(loop_answers.path(self.manifest, second).exists())
        self.respond(self.answers(first))
        self.quiet(questions.anx_answers, self.manifest)
        self.quiet(reconcile.reconcile, self.manifest, at=10000)
        self.quiet(reconcile.reconcile, self.manifest, at=20000)
        rows = [r for r in self.rows() if r['id'] == 'answer-' + first]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['text'], 'Pause until reviewed')
        self.assertEqual(read_json(loop_answers.path(self.manifest, first))['answer']['text'], 'Pause until reviewed')
        self.respond(self.answers(second))
        self.quiet(questions.anx_answers, self.manifest)
        self.assertEqual(len([r for r in self.rows() if r['id'] == 'answer-' + second]), 1)

    def test_bus_failure_retains_answer_and_reconcile_drains_without_another_remote_delivery(self):
        ask_id = self.record_failure()['ask_id']
        item = self.answers(ask_id)['json']['result']['answers'][0]
        pending = loop_answers.path(self.manifest, ask_id).with_suffix('.answer.json')
        with patch.object(loop_answers.bus, 'main', side_effect=SystemExit(1)):
            self.assertTrue(self.quiet(loop_answers.receive, self.manifest, item))
        self.assertEqual(read_json(pending)['answer'], item['answer'])
        with patch.object(reconcile.AgentctlClient, 'recent', side_effect=ToolError('agentctl', 6, 'unavailable')), \
                self.assertRaises(ToolError):
            self.quiet(reconcile.reconcile, self.manifest)
        self.assertFalse(pending.exists())
        self.assertEqual(len([r for r in self.rows() if r['id'] == 'answer-' + ask_id]), 1)

    def test_registration_failure_retries_locally_without_creating_another_ask(self):
        with patch.object(loop_answers, 'remember', side_effect=OSError('local write failed')):
            report = self.record_failure()
        self.assertFalse(report['sent'])
        ask_id = report['ask_id']
        self.quiet(loop.retry_escalation, self.manifest, 'audit', 'checks-audit', self.state)
        self.assertTrue(read_json(self.state/'escalation.json')['sent'])
        self.assertTrue(loop_answers.path(self.manifest, ask_id).exists())
        self.assertEqual(read_json(loop_answers.path(self.manifest, ask_id))['card'], 'card:loop-audit')
        self.assertEqual(len([c for c in self.calls() if c['tool'] == 'anx' and 'ask' in c['args']]), 1)

    def test_missing_ask_id_is_not_a_confirmed_correlatable_delivery(self):
        for result in ({}, None, {'body': None}):
            self.respond({'tool': 'anx', 'contains': ['ask'], 'json': {'ok': True, 'result': result}})
            report = self.record_failure()
            self.assertFalse(report['sent'])
            self.assertNotIn('next_at', report)
            self.assertNotIn('ask_id', report)

    def test_unrelated_answer_is_not_claimed_by_subject_alone(self):
        self.record_failure()
        self.assertFalse(loop_answers.receive(self.manifest, {
            'ask_id': 'event:unrelated', 'subject_ref': 'card:loop-audit', 'answer': {'text': 'yes'}}))
        self.assertFalse(self.manifest.paths.bus_log.exists())

    def test_question_reader_preserves_reply_before_producer_registration(self):
        ask_id = 'event:early-loop-reply'
        self.respond(self.answers(ask_id))
        self.quiet(questions.anx_answers, self.manifest)
        self.assertEqual(len(loop_answers.deferred(self.manifest)), 1)
        # The producer returns from ANX and records its ID after the shared reader.
        loop_answers.remember(self.manifest, ask_id, 'card:loop-audit', 'audit', 'cycle-one')
        self.respond()  # the consuming stream will not return this answer again
        self.quiet(reconcile.reconcile, self.manifest)
        self.assertEqual(len([r for r in self.rows() if r['id'] == 'answer-' + ask_id]), 1)
        self.assertEqual(loop_answers.deferred(self.manifest), [])
        self.assertEqual(read_json(self.manifest.paths.root/'reconcile/answers.json'), [])

    def test_question_reader_preserves_another_producers_blocker_answer(self):
        bus.configure(self.manifest)
        bus.append([dict(kind='blocker', id='blocker-one', origin='coordkit', frm='worker', owner='lead',
                         to='lead', subject='card:task', text='Need a decision', ts='2026-01-01T00:00:00Z')], project=False)
        (self.manifest.paths.root/'state').mkdir(exist_ok=True)
        (self.manifest.paths.root/'state/mirror.cursor').write_text('1')
        response = self.answers('event:blocker-ask')
        response['json']['result']['answers'][0]['subject_ref'] = 'card:task'
        self.respond(response)
        self.quiet(questions.anx_answers, self.manifest)
        self.respond()
        self.quiet(reconcile.reconcile, self.manifest)
        decisions = [r for r in self.rows() if r['kind'] == 'decision' and not r.get('copy_of')]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]['re'], 'blocker-one')
        self.assertEqual(read_json(self.manifest.paths.root/'reconcile/answers.json'), [])

    def test_failed_merge_retains_shared_reader_handoff(self):
        loop_answers.defer(self.manifest, {'ask_id': 'event:early', 'answer': {'text': 'yes'}})
        def fail_pending(path, value):
            if path.name == 'answers.json':
                raise OSError('pending queue unavailable')
            write_json(path, value)
        with patch.object(reconcile, 'write_json', side_effect=fail_pending), self.assertRaises(OSError):
            self.quiet(reconcile.reconcile, self.manifest)
        self.assertEqual(len(loop_answers.deferred(self.manifest)), 1)

    def test_lead_waiter_wins_shared_stream_and_still_routes_loop_decision(self):
        ask_id = self.record_failure()['ask_id']
        self.manifest.escalation.internal_to = 'worker'
        self.quiet(wait.main, ['pending'], self.manifest)
        self.respond(self.answers(ask_id))
        found = self.quiet(wait.scan_anx, set())
        self.assertTrue(any('anx-answer' in line for line in found))
        decisions = [r for r in self.rows() if r['id'] == 'answer-' + ask_id]
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]['to'], 'worker')
        self.respond()  # no later consumer can get this reply from ANX again
        self.quiet(reconcile.reconcile, self.manifest)
        self.assertEqual(loop_answers.deferred(self.manifest), [])
        self.assertEqual(read_json(self.manifest.paths.root/'reconcile/answers.json'), [])

    def test_supported_ask_response_shapes_are_confirmed_without_resending(self):
        for index, field in enumerate(('ask_id', 'id', 'event_ref', 'ask', 'body', 'mixed', 'wrapped')):
            ask_id = f'event:shape-{index}'
            result = {'ask': {'id': ask_id}} if field == 'ask' else (
                {'body': {'ask': {'id': ask_id}}} if field == 'body' else (
                    {'ask_id': ask_id, 'body': {'text': 'context'}} if field == 'mixed' else (
                        {'id': 'event:wrapper', 'body': {'ask_id': ask_id}} if field == 'wrapped' else {field: ask_id})))
            (self.state/'escalation.json').unlink(missing_ok=True)
            self.respond({'tool': 'anx', 'contains': ['ask'], 'json': {'ok': True, 'result': result}})
            report = self.record_failure()
            self.assertTrue(report['sent'])
            self.assertEqual(report['ask_id'], ask_id)
            before = len([c for c in self.calls() if c['tool'] == 'anx' and 'ask' in c['args']])
            self.record_failure()
            self.assertEqual(len([c for c in self.calls() if c['tool'] == 'anx' and 'ask' in c['args']]), before)

    def test_client_captures_reordered_answer_flags_without_polluting_stdout(self):
        ask_id = self.record_failure()['ask_id']
        self.respond(self.answers(ask_id))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = AnxClient(self.manifest).call('await', '--timeout', '1s', '--answers')
        self.assertEqual(out.getvalue(), '')
        self.assertEqual(result['answers'][0]['ask_id'], ask_id)
        self.assertEqual(self.rows()[0]['id'], 'answer-' + ask_id)

    def test_shadow_answer_read_does_not_persist_or_relay(self):
        ask_id = self.record_failure()['ask_id']
        self.respond(self.answers(ask_id))
        self.manifest.mode = 'shadow'
        before = {p: p.read_bytes() for p in self.manifest.paths.root.rglob('*') if p.is_file()}
        result = AnxClient(self.manifest).call('await', '--answers')
        self.assertEqual(result['answers'][0]['ask_id'], ask_id)
        self.assertEqual(before, {p: p.read_bytes() for p in self.manifest.paths.root.rglob('*') if p.is_file()})
