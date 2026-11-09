"""A read acknowledgement must never be the first durable handling of a reply."""
import contextlib
import io
import time
from unittest.mock import patch

from coordkit import answers, bus, reconcile
from coordkit.manifest import load, ManifestError
from coordkit.state import read_json
from tests.support import FakeCase


class AnswerConsumptionTests(FakeCase):
    def setUp(self):
        super().setUp()
        self.manifest.bus.mirror = False
        self.item = {'ask_id': 'event:operator-reply', 'subject_ref': 'card:loop-audit',
                     'answer_unread': True, 'answer': {'text': 'Repair it now', 'response_event_ref': 'event:response'}}

    def listing(self, items=None, **extra):
        return {'tool': 'anx', 'contains': ['inbox', 'list', '--unread'],
                'json': {'ok': True, 'result': {'items': [self.item] if items is None else items, **extra}}}

    def consume(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            answers.consume(self.manifest)

    def rows(self):
        bus.configure(self.manifest)
        return [r for r in bus.read() if not r.get("copy_of")]

    def reads(self):
        return [c for c in self.calls() if c['tool'] == 'anx' and '/agent-inbox/answers/read' in c['args']]

    def test_own_unread_answers_are_durably_delivered_then_marked_read_once(self):
        self.respond(self.listing())
        original = answers.AnxClient.call
        def check(client, *args, **kwargs):
            if args[:2] == ('api', 'call'):
                self.assertEqual(self.rows()[0]['text'], 'Repair it now')
                self.assertEqual(read_json(answers.receipt_path(self.manifest, self.item))['phase'], 'delivered')
                self.assertIn('/agent-inbox/answers/read', args)
                import json
                from pathlib import Path
                self.assertEqual(json.loads(Path(args[args.index('--from-file')+1]).read_text()), {'answer_event_id': 'response'})
            return original(client, *args, **kwargs)
        with patch.object(answers.AnxClient, 'call', check):
            self.consume()
        self.respond(self.listing([]))
        self.consume()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.reads()), 1)
        row = self.rows()[0]
        self.assertEqual((row['kind'], row['to'], row['subject']), ('decision', 'coordinator', 'card:loop-audit'))
        self.assertNotIn('WARN', answers.health(self.manifest))
        args = self.calls()[0]['args']
        self.assertIn('--unread', args)
        self.assertIn(self.manifest.anx.lead_actor, args)

    def test_bus_failure_keeps_answer_unread_and_restart_retries(self):
        self.respond(self.listing())
        with patch.object(answers.bus, 'main', side_effect=SystemExit(1)):
            self.consume()
        self.assertEqual(self.reads(), [])
        self.assertIn('WARN', answers.health(self.manifest))
        self.respond(self.listing([]))
        self.consume()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.reads()), 1)

    def test_read_failure_retries_without_duplicate_bus_delivery(self):
        self.respond(self.listing(), {'tool': 'anx', 'contains': ['/agent-inbox/answers/read'], 'exit': 6})
        self.consume()
        self.assertEqual(len(self.rows()), 1)
        self.assertIn('WARN', answers.health(self.manifest))
        failed_reads = len(self.reads())
        self.assertGreaterEqual(failed_reads, 1)
        self.respond(self.listing([]))
        self.consume()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.reads()), failed_reads + 1)
        self.assertNotIn('WARN', answers.health(self.manifest))

    def test_append_then_crash_is_deduped_before_ack(self):
        self.respond(self.listing())
        original = answers.bus.main
        def append_then_fail(*args):
            original(*args)
            raise OSError('crash after append')
        with patch.object(answers.bus, 'main', side_effect=append_then_fail):
            self.consume()
        self.assertFalse(self.reads())
        self.consume()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.reads()), 1)

    def test_unrouteable_reply_is_retained_and_visible_without_mark_read(self):
        del self.item['subject_ref']
        self.respond(self.listing())
        self.consume()
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.reads(), [])
        self.assertIn('pending=1', answers.health(self.manifest))

    def test_truncated_or_cyclic_snapshot_never_acknowledges_partial_listing(self):
        self.respond(self.listing(next_cursor='same', has_more=True))
        self.consume()
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.reads(), [])
        self.assertIn('poll_error=ValueError', answers.health(self.manifest))

    def test_long_reply_is_kept_in_full_before_read(self):
        self.item['answer']['text'] = 'Full answer ' * 100
        self.respond(self.listing())
        self.consume()
        row = self.rows()[0]
        from pathlib import Path
        self.assertEqual(Path(row['spill_ref']).read_text(), self.item['answer']['text'] + '\n')
        self.assertEqual(len(self.reads()), 1)

    def test_workspace_actor_scope_does_not_replay_another_actors_queue(self):
        self.respond(self.listing())
        with patch.object(answers.bus, 'main', side_effect=SystemExit(1)):
            self.consume()
        old = answers.home(self.manifest)
        self.manifest.anx.lead_actor = 'another-actor'
        self.respond(self.listing([]))
        self.consume()
        self.assertNotEqual(old, answers.home(self.manifest))
        self.assertEqual(self.rows(), [])
        self.assertFalse(self.reads())

    def test_reconciler_routes_answers_before_unrelated_probe_failure(self):
        self.respond(self.listing(), {'tool': 'agentctl', 'contains': ['recent'], 'exit': 6})
        from coordkit.process import ToolError
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(ToolError):
            reconcile.reconcile(self.manifest)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.reads()), 1)

    def test_closed_question_answer_keeps_exact_asker_and_reply_edge(self):
        from coordkit import questions
        from coordkit.state import write_json
        write_json(questions.home(self.manifest)/'questions.json', {'q-123abc': {
            'qid': 'q-123abc', 'state': 'closed', 'created_at': time.time(), 'closed_at': time.time(),
            'anx_ask_id': self.item['ask_id'], 'asker': 'worker'}})
        self.respond(self.listing())
        self.consume()
        row = self.rows()[0]
        self.assertEqual((row['to'], row['re']), ('worker', 'q-123abc'))
        self.assertEqual(questions.load_all(self.manifest)['q-123abc']['state'], 'closed')
        self.assertEqual(len(self.reads()), 1)

    def test_doctor_surfaces_unrouteable_answer(self):
        from coordkit import lifecycle
        self.item.pop('subject_ref')
        self.respond(self.listing())
        self.consume()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            lifecycle.doctor(self.manifest)
        self.assertIn('DOCTOR unread_answers warn', out.getvalue())

    def test_newer_answer_to_same_ask_gets_its_own_delivery(self):
        self.respond(self.listing())
        self.consume()
        self.item['answer'] = {'text': 'Use the new plan', 'response_event_id': 'new-response'}
        self.respond(self.listing())
        self.consume()
        self.assertEqual({row['text'] for row in self.rows()}, {'Repair it now', 'Use the new plan'})
        self.assertEqual(len(self.reads()), 2)

    def test_shared_answer_stream_does_not_redeliver_a_handled_response(self):
        self.respond(self.listing())
        self.consume()
        self.respond({'tool': 'anx', 'contains': ['await'], 'json': {'ok': True, 'result': {'answers': [self.item]}}})
        from coordkit.anxcli import AnxClient
        self.assertEqual(AnxClient(self.manifest).call('await', '--answers')['answers'], [])
        self.assertEqual(len(self.rows()), 1)

    def test_empty_read_response_is_not_accepted_as_acknowledgement(self):
        self.respond(self.listing(), {'tool': 'anx', 'contains': ['/agent-inbox/answers/read'],
                                     'json': {'ok': True, 'result': {}}})
        self.consume()
        self.assertEqual(len(self.rows()), 1)
        self.assertIn('pending=1', answers.health(self.manifest))

    def test_card_level_answer_never_closes_an_unrelated_blocker(self):
        bus.configure(self.manifest)
        bus.append([{'id': 'unrelated-blocker', 'kind': 'blocker', 'origin': 'coordkit',
                     'frm': 'worker', 'to': 'lead', 'owner': 'lead', 'subject': 'card:loop-audit',
                     'needs': 'A different decision', 'text': 'A different issue',
                     'ts': '2026-01-01T00:00:00Z'}], project=False)
        self.respond(self.listing())
        self.consume()
        row = next(r for r in self.rows() if r['kind'] == 'decision')
        self.assertEqual(row['to'], 'coordinator')
        self.assertNotIn('re', row)
        self.assertEqual(len(bus.open_blockers(self.rows())), 1)

    def test_shadow_pass_does_not_poll_or_ack(self):
        self.manifest.mode = 'shadow'
        with self.assertRaises(ManifestError):
            self.consume()
        self.assertEqual(self.calls(), [])


class LoopSelectorTests(FakeCase):
    def test_family_is_resolved_by_agentctl_with_read_only_permissions(self):
        from coordkit import loop
        self.file.write_text(self.file.read_text() + '\n[loop.loops.audit.selector]\nfamily = "glm-flash"\n')
        self.manifest = load(self.file)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(loop.run_cycle(self.manifest, 'audit'), 0)
        calls = [c for c in self.calls() if c['tool'] == 'agentctl' and 'delegate' in c['args']]
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertEqual(call['request']['selector'], {'family': 'glm-flash', 'settings': {'access': 'read_only'}})
        self.assertEqual(calls[0]['request'], calls[1]['request'])

    def test_selector_cannot_override_access_or_mix_pool_and_family(self):
        for text in ('family="glm-flash"\npool="plan"', 'family="glm-flash"\nsettings={access="coding"}', 'harness="zcode"'):
            with self.subTest(text=text):
                self.file.write_text('[project]\nname="sample"\ngit_repo="owner/repo"\n[loop.loops.audit.selector]\n' + text)
                with self.assertRaises(ManifestError):
                    load(self.file)
