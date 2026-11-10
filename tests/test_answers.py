"""A read acknowledgement must never be the first durable handling of a reply."""
import contextlib
import io
import sys
import time
from unittest.mock import patch

from coordkit import answers, bus, reconcile
from coordkit.manifest import load, ManifestError
from coordkit.state import read_json, write_json
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

    def test_archiving_dispositioned_invalid_receipt_clears_quarantine_warning(self):
        bad = dict(self.item, ask_id='event:bad/ask')
        self.respond(self.listing([bad]))
        self.consume()
        self.assertIn('invalid=1', answers.health(self.manifest))
        with answers.delivery_lock(self.manifest) as acquired:
            self.assertTrue(acquired)
            directory = answers.home(self.manifest)
            archive = directory/'archive'
            archive.mkdir()
            receipt, = directory.glob('invalid-*.json')
            self.assertEqual(read_json(receipt)['phase'], 'invalid')
            receipt.rename(archive/receipt.name)
        self.respond(self.listing([]))  # the bad source item has been repaired/dispositioned
        self.consume()
        self.assertNotIn('WARN', answers.health(self.manifest))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.reads(), [])

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

    def test_reply_between_unread_snapshot_and_shared_stream_is_published_once(self):
        from coordkit import loop_answers
        from coordkit.anxcli import AnxClient
        loop_answers.remember(self.manifest, self.item['ask_id'], 'card:loop-audit', 'audit', 'cycle')
        self.respond(self.listing([]))
        self.consume()  # reply arrives only after the requester's unread snapshot
        self.respond(self.listing(), {'tool': 'anx', 'contains': ['await'],
                     'json': {'ok': True, 'result': {'answers': [self.item]}}})
        original = answers.bus.main
        def race(*args):
            self.consume()  # the other publisher must respect the stream's lock
            return original(*args)
        with patch.object(answers.bus, 'main', side_effect=race), contextlib.redirect_stderr(io.StringIO()):
            AnxClient(self.manifest).call('await', '--answers')
        self.consume()  # even a stale unread listing cannot republish or re-ack it
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            reconcile.reconcile(self.manifest)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.reads()), 1)
        self.assertEqual(read_json(answers.receipt_path(self.manifest, self.item))['phase'], 'read')
        self.assertFalse(loop_answers.path(self.manifest, self.item['ask_id']).with_suffix('.answer.json').exists())

    def test_subjectless_stream_reply_retries_after_loop_correlation_arrives(self):
        from coordkit import loop_answers
        from coordkit.anxcli import AnxClient
        self.item.pop('subject_ref')
        self.respond({'tool': 'anx', 'contains': ['await'],
                      'json': {'ok': True, 'result': {'answers': [self.item]}}})
        AnxClient(self.manifest).call('await', '--answers')
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.reads(), [])
        loop_answers.remember(self.manifest, self.item['ask_id'], 'card:loop-audit', 'audit', 'cycle')
        self.respond()  # the shared stream will never return this response again
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            reconcile.reconcile(self.manifest)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['subject'], 'card:loop-audit')
        self.assertEqual(len(self.reads()), 1)
        self.assertEqual(read_json(answers.receipt_path(self.manifest, self.item))['phase'], 'read')

    def test_subjectless_reply_can_be_routed_by_later_question_correlation(self):
        from coordkit import questions
        self.item.pop('subject_ref')
        self.respond(self.listing())
        self.consume()
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.reads(), [])
        record = self.question()
        with contextlib.redirect_stderr(io.StringIO()):
            questions.relay_anx_answer(self.manifest, record, self.item['answer'])
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual((self.rows()[0]['to'], self.rows()[0]['re']), ('worker', 'q-123abc'))
        self.assertEqual(len(self.reads()), 1)
        # Stale subjectless data and later metadata cannot reopen the read receipt.
        self.consume()
        with contextlib.redirect_stderr(io.StringIO()):
            questions.relay_anx_answer(self.manifest, record, self.item['answer'])
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.reads()), 1)

    def test_invalid_empty_reply_keeps_quarantine_evidence_after_full_reply_delivered(self):
        self.item['answer'] = {'response_event_id': 'response'}
        self.respond(self.listing())
        self.consume()
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.reads(), [])
        self.item['answer'] = {'response_event_ref': 'event:response', 'text': 'Repair it now'}
        self.respond(self.listing())
        self.consume()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['text'], 'Repair it now')
        self.assertEqual(len(self.reads()), 1)
        self.assertIn('invalid=1', answers.health(self.manifest))  # keep the original invalid evidence

    def test_partial_pending_receipt_cannot_replace_an_existing_publication(self):
        from coordkit.state import write_json
        self.respond(self.listing())
        self.consume()
        path = answers.receipt_path(self.manifest, self.item)
        # A damaged/old receipt must not change the payload of an existing row.
        record = read_json(path)
        record['phase'] = 'pending'
        record['item']['answer'] = {'response_event_id': 'response'}
        write_json(path, record)
        self.item['answer'] = {'response_event_id': 'response', 'text': 'A replacement'}
        self.respond(self.listing())
        self.consume()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['text'], 'Repair it now')
        self.assertEqual(len(self.reads()), 1)
        self.assertIn('invalid=', answers.health(self.manifest))

    def test_question_sweep_metadata_variants_do_not_reopen_read_response(self):
        from coordkit import questions
        record = self.question()
        self.respond(self.listing())
        self.consume()
        for text in ('Repair it now', 'A differently rendered answer'):
            alternate = {'text': text, 'response_event_id': 'response', 'outcome': 'answered'}
            with contextlib.redirect_stderr(io.StringIO()):
                questions.relay_anx_answer(self.manifest, record, alternate)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.reads()), 1)
        receipt = read_json(answers.receipt_path(self.manifest, self.item))
        self.assertEqual(receipt['phase'], 'read')
        self.assertEqual(receipt['item']['answer'], self.item['answer'])

    def test_question_sweep_retries_delivered_response_using_its_saved_payload(self):
        from coordkit import questions
        record = self.question()
        self.respond(self.listing(), {'tool': 'anx', 'contains': ['/agent-inbox/answers/read'], 'exit': 6})
        self.consume()
        self.assertEqual(read_json(answers.receipt_path(self.manifest, self.item))['phase'], 'delivered')
        self.respond()
        alternate = {'text': 'A differently rendered answer', 'response_event_id': 'response'}
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(questions.relay_anx_answer(self.manifest, record, alternate))
        self.assertEqual(len(self.rows()), 1)
        receipt = read_json(answers.receipt_path(self.manifest, self.item))
        self.assertEqual(receipt['phase'], 'read')
        self.assertEqual(receipt['item']['answer'], self.item['answer'])

    def test_known_question_supplies_card_for_subjectless_shared_reply(self):
        from coordkit.anxcli import AnxClient
        self.question()
        self.item.pop('subject_ref')
        self.respond({'tool': 'anx', 'contains': ['await'],
                      'json': {'ok': True, 'result': {'answers': [self.item]}}})
        with contextlib.redirect_stderr(io.StringIO()):
            AnxClient(self.manifest).call('await', '--answers')
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual((self.rows()[0]['subject'], self.rows()[0]['re']), ('card:loop-audit', 'q-123abc'))
        self.assertEqual(len(self.reads()), 1)

    def test_loop_stream_without_exact_response_id_uses_legacy_publication(self):
        from coordkit import loop_answers
        from coordkit.anxcli import AnxClient
        loop_answers.remember(self.manifest, self.item['ask_id'], 'card:loop-audit', 'audit', 'cycle')
        self.item['answer'] = {'text': 'A legacy reply without an event ID'}
        self.respond({'tool': 'anx', 'contains': ['await'],
                      'json': {'ok': True, 'result': {'answers': [self.item]}}})
        AnxClient(self.manifest).call('await', '--answers')
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['id'], 'answer-' + self.item['ask_id'])
        self.assertEqual(self.rows()[0]['text'], self.item['answer']['text'])
        self.assertEqual(self.reads(), [])
        self.assertNotIn('WARN', answers.health(self.manifest))
        self.assertFalse(loop_answers.path(self.manifest, self.item['ask_id']).with_suffix('.answer.json').exists())

    def legacy_reply(self):
        from coordkit import loop_answers
        loop_answers.remember(self.manifest, self.item['ask_id'], 'card:loop-audit', 'audit', 'cycle')
        item = dict(self.item, answer={'text': 'Repair it now'})
        with contextlib.redirect_stderr(io.StringIO()):
            loop_answers.receive(self.manifest, item)
        return loop_answers.path(self.manifest, self.item['ask_id']).with_suffix('.answer.json')

    def test_subjectless_identified_question_reply_reuses_prior_legacy_delivery(self):
        from coordkit import questions
        self.legacy_reply()
        record = self.question()
        record['asker'] = 'coordinator'
        write_json(questions.home(self.manifest)/'questions.json', {record['qid']: record})
        self.item.pop('subject_ref')
        self.respond(self.listing())
        self.consume()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['id'], 'answer-' + self.item['ask_id'])
        self.assertEqual(len(self.reads()), 1)
        receipt = answers.receipt_path(self.manifest, self.item)
        self.assertEqual(read_json(receipt)['phase'], 'read')
        self.assertEqual(read_json(receipt.with_suffix('.read-body')), {'answer_event_id': 'response'})
        self.assertEqual(questions.load_all(self.manifest)[record['qid']]['state'], 'answered')
        self.assertNotIn('WARN', answers.health(self.manifest))

    def test_response_less_unread_reply_publishes_once_without_source_ack(self):
        self.item['answer'] = {'text': 'Repair it now'}
        self.respond(self.listing())
        self.consume()
        self.consume()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['id'], 'answer-' + self.item['ask_id'])
        receipt = read_json(answers.receipt_path(self.manifest, self.item))
        self.assertEqual(receipt['phase'], 'legacy-delivered')
        self.assertIs(receipt['source_read'], False)
        self.assertEqual(self.reads(), [])

    def test_response_less_inbox_append_crash_then_exact_reply_deduplicates_without_loop_provenance(self):
        legacy = dict(self.item, answer={'text': 'Repair it now'})
        self.respond(self.listing([legacy]))
        original = bus.main
        def append_then_fail(*args):
            original(*args)
            raise OSError('crash after append, before compatibility receipt')
        with patch.object(bus, 'main', side_effect=append_then_fail):
            self.consume()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.reads(), [])
        self.respond(self.listing([legacy, self.item]))
        self.consume()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.reads()), 1)
        self.assertEqual(read_json(answers.receipt_path(self.manifest, legacy))['phase'], 'legacy-delivered')

    def test_empty_unread_answer_is_quarantined_without_blocking_next_reply(self):
        empty = dict(self.item, ask_id='event:empty-inbox', answer={})
        self.respond(self.listing([empty, self.item]))
        self.consume()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['text'], 'Repair it now')
        self.assertEqual(len(self.reads()), 1)
        invalid = [read_json(p) for p in answers.home(self.manifest).glob('invalid-*.json')]
        self.assertEqual([r['item'] for r in invalid], [empty])
        self.assertIn('invalid=1', answers.health(self.manifest))

    def test_failed_new_receipt_write_does_not_abort_other_answers_and_retries_next_sweep(self):
        failed = dict(self.item, ask_id='event:write-failed',
                      answer={'text': 'Retain this too', 'response_event_id': 'write-response'})
        self.respond(self.listing([failed, self.item]))
        original = answers.write_json
        def write(path, record):
            if path == answers.receipt_path(self.manifest, failed):
                raise OSError('transient receipt write failure')
            return original(path, record)
        with patch.object(answers, 'write_json', side_effect=write):
            self.consume()
        self.assertFalse(answers.receipt_path(self.manifest, failed).exists())
        self.assertEqual([r['text'] for r in self.rows()], ['Repair it now'])
        self.assertEqual(len(self.reads()), 1)
        self.assertIn('poll_error=OSError', answers.health(self.manifest))
        self.respond(self.listing([failed, self.item]))
        self.consume()
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(len(self.reads()), 2)
        self.assertEqual(read_json(answers.receipt_path(self.manifest, failed))['phase'], 'read')
        self.assertNotIn('WARN', answers.health(self.manifest))

    def test_revised_response_less_unread_reply_has_distinct_retry_stable_id(self):
        self.item['answer'] = {'text': 'First ruling'}
        self.respond(self.listing())
        self.consume()
        self.item['answer'] = {'text': 'Revised ruling'}
        self.respond(self.listing())
        original = bus.main
        def append_then_fail(*args):
            original(*args)
            raise OSError('crash after append')
        with patch.object(bus, 'main', side_effect=append_then_fail):
            self.consume()
        self.consume()
        self.consume()
        self.assertEqual({row['text'] for row in self.rows()}, {'First ruling', 'Revised ruling'})
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(len({row['id'] for row in self.rows()}), 2)
        self.assertEqual(self.reads(), [])
        # The revised row must also bind to its own later exact response once.
        self.item['answer']['response_event_id'] = 'revision-response'
        self.respond(self.listing())
        self.consume()
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(len(self.reads()), 1)

    def test_unchanged_legacy_snapshot_scans_retained_receipts_and_bus_once_per_sweep(self):
        items = [dict(self.item, ask_id=f'event:batch-{i}', answer={'text': f'Ruling {i}'}) for i in range(8)]
        self.respond(self.listing(items))
        self.consume()
        for i in range(75):
            write_json(answers.home(self.manifest)/f'retained-{i}.json', {'phase': 'read'})
        retained = len(list(answers.home(self.manifest).glob('*.json')))
        with patch.object(answers, 'read_json', wraps=read_json) as reads, \
                patch.object(bus, 'read', wraps=bus.read) as bus_reads, \
                patch.object(answers, 'legacy_pending', wraps=answers.legacy_pending) as index, \
                patch.object(answers, 'legacy_match', side_effect=AssertionError('terminal item must not rescan')):
            self.consume()
        self.assertEqual(bus_reads.call_count, 1)
        self.assertEqual(index.call_count, 1)
        self.assertLessEqual(reads.call_count, retained + len(items) + 1)
        self.assertEqual(len(self.rows()), len(items))
        self.assertEqual(self.reads(), [])

    def test_identified_sweep_reuses_one_bus_snapshot_until_each_append(self):
        items = [dict(self.item, ask_id=f'event:new-{i}',
                      answer={'text': f'Ruling {i}', 'response_event_id': f'response-{i}'}) for i in range(3)]
        self.respond(self.listing(items))
        original, callers = bus.read, []
        def read():
            # Bus writers retain their own authoritative reads. The consumer
            # must use its snapshot everywhere except after a possible append.
            frame = sys._getframe(1)
            while frame.f_code.co_filename.endswith('/unittest/mock.py'):
                frame = frame.f_back
            if frame.f_code.co_filename == answers.__file__:
                callers.append(frame.f_code.co_name)
            return original()
        with patch.object(bus, 'read', side_effect=read):
            self.consume()
        self.assertEqual(callers, ['refresh'] * (1 + len(items)))
        self.assertEqual(len(self.rows()), len(items))
        self.assertEqual(len(self.reads()), len(items))

    def interrupted_pre_upgrade_loop_reply(self):
        """Old releases appended ask-only rows before deleting the pending file."""
        from coordkit import loop_answers
        loop_answers.remember(self.manifest, self.item['ask_id'], 'card:loop-audit', 'audit', 'cycle')
        provenance = loop_answers.path(self.manifest, self.item['ask_id'])
        record = read_json(provenance)
        record['answer'] = self.item['answer']
        pending = provenance.with_suffix('.answer.json')
        write_json(pending, record)
        bus.configure(self.manifest)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            bus.main(['post', '--from', 'coordkit', '--to', 'coordinator', '--kind', 'decision',
                      '--topic', 'loop-failure-answer', '--subject', record['card'],
                      '--rollback', 'Revise the answer on its original ANX ask.',
                      '--id', 'answer-' + record['ask_id'], '--force', '--no-mirror',
                      '--', record['answer']['text']], self.manifest)
        # Seed only the old release's artifacts, not new compatibility receipts.
        self.assertFalse(answers.legacy_path(self.manifest, self.item['ask_id']).exists())
        self.assertFalse(answers.receipt_path(self.manifest, self.item).exists())
        return pending

    def assert_upgraded_loop_reply(self, pending):
        self.assertFalse(pending.exists())
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['id'], 'answer-' + self.item['ask_id'])
        receipt = answers.receipt_path(self.manifest, self.item)
        self.assertEqual(read_json(receipt)['phase'], 'read')
        self.assertEqual(read_json(receipt.with_suffix('.read-body')), {'answer_event_id': 'response'})
        self.assertEqual(len(self.reads()), 1)

    def test_upgrade_drain_reuses_legacy_publication_and_acks_exact_response(self):
        from coordkit import loop_answers
        pending = self.interrupted_pre_upgrade_loop_reply()
        with patch.object(bus, 'main', side_effect=AssertionError('must reuse legacy row')):
            loop_answers.drain(self.manifest)
            loop_answers.drain(self.manifest)
        self.assert_upgraded_loop_reply(pending)

    def test_upgrade_shared_answer_reuses_legacy_publication_and_acks_exact_response(self):
        from coordkit import loop_answers
        pending = self.interrupted_pre_upgrade_loop_reply()
        with patch.object(bus, 'main', side_effect=AssertionError('must reuse legacy row')):
            self.assertTrue(answers.shared_answer(self.manifest, self.item))
            self.assertTrue(answers.shared_answer(self.manifest, self.item))
            loop_answers.drain(self.manifest)
        self.assert_upgraded_loop_reply(pending)

    def test_upgrade_drain_retains_pending_until_exact_ack_succeeds_without_reposting(self):
        from coordkit import loop_answers
        pending = self.interrupted_pre_upgrade_loop_reply()
        self.respond({'tool': 'anx', 'contains': ['/agent-inbox/answers/read'], 'exit': 6})
        with patch.object(bus, 'main', side_effect=AssertionError('must reuse legacy row')):
            loop_answers.drain(self.manifest)
            self.assertTrue(pending.exists())
            self.assertEqual(len(self.rows()), 1)
            self.assertEqual(read_json(answers.receipt_path(self.manifest, self.item))['phase'], 'delivered')
            failed_reads = len(self.reads())
            self.assertGreater(failed_reads, 0)
            self.respond()
            loop_answers.drain(self.manifest)
            loop_answers.drain(self.manifest)
        self.assertFalse(pending.exists())
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.reads()), failed_reads + 1)
        self.assertEqual(read_json(answers.receipt_path(self.manifest, self.item))['phase'], 'read')

    def test_legacy_then_identified_reply_reuses_one_row_but_distinct_events_stay_distinct(self):
        self.assertFalse(self.legacy_reply().exists())
        self.respond(self.listing())
        self.consume()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.reads()), 1)
        self.assertEqual(self.rows()[0]['id'], 'answer-' + self.item['ask_id'])
        self.item['answer'] = {'text': 'Repair it now', 'response_event_id': 'another-response'}
        self.respond(self.listing())
        self.consume()
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(len(self.reads()), 2)
        binding = read_json(answers.legacy_path(self.manifest, self.item['ask_id']))
        self.assertEqual(binding['response_event_id'], 'response')

    def test_identified_then_legacy_reply_reuses_canonical_row(self):
        self.respond(self.listing())
        self.consume()
        self.assertFalse(self.legacy_reply().exists())
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['id'], answers.publication_id(self.item))
        self.assertEqual(len(self.reads()), 1)
        self.item['answer'] = {'text': 'Repair it now', 'response_event_id': 'another-response'}
        self.respond(self.listing())
        self.consume()
        self.assertFalse(self.legacy_reply().exists())
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(len(self.reads()), 2)

    def test_unread_poll_first_legacy_delivery_settles_local_receipt_without_false_read_ack(self):
        full = self.item['answer']
        self.item['answer'] = {'text': 'Repair it now'}
        self.respond(self.listing())
        self.consume()
        self.assertIn('source_read_unconfirmed=1', answers.health(self.manifest))
        self.assertFalse(self.legacy_reply().exists())
        self.assertNotIn('WARN', answers.health(self.manifest))
        self.assertIn('source_read_unconfirmed=1', answers.health(self.manifest))
        self.assertEqual(self.reads(), [])
        local = read_json(answers.receipt_path(self.manifest, self.item))
        self.assertEqual(local['phase'], 'legacy-delivered')
        self.assertIs(local['source_read'], False)
        self.item['answer'] = {'text': 'Repair it now', 'outcome': 'answered'}
        self.respond(self.listing())
        self.consume()  # another response-less shape must not recreate a warning
        self.assertNotIn('WARN', answers.health(self.manifest))
        self.item['answer'] = full
        self.respond(self.listing())
        self.consume()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.reads()), 1)
        self.assertIn('source_read_unconfirmed=0', answers.health(self.manifest))

    def test_identified_delivery_settles_prior_response_less_poll_without_shared_stream(self):
        full = self.item['answer']
        self.item['answer'] = {'text': 'Repair it now'}
        self.respond(self.listing())
        self.consume()
        self.assertIn('source_read_unconfirmed=1', answers.health(self.manifest))
        self.item['answer'] = full
        self.respond(self.listing())
        self.consume()
        self.assertNotIn('WARN', answers.health(self.manifest))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.reads()), 1)
        self.assertIn('source_read_unconfirmed=0', answers.health(self.manifest))

    def test_non_object_quarantine_does_not_block_delivery_or_local_settlement(self):
        self.respond(self.listing([None, self.item]))
        self.consume()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.reads()), 1)
        self.assertIn('invalid=1', answers.health(self.manifest))

    def test_response_less_receipts_keep_conflicting_cards_separate(self):
        alpha = dict(self.item, subject_ref='card:alpha', answer={'text': 'Repair it now'})
        beta = dict(alpha, subject_ref='card:beta')
        self.respond(self.listing([alpha, beta]))
        self.consume()
        self.assertNotEqual(answers.receipt_path(self.manifest, alpha), answers.receipt_path(self.manifest, beta))
        self.assertEqual({row['subject'] for row in self.rows()}, {'card:alpha', 'card:beta'})
        self.assertIn('legacy_local_deliveries=2', answers.health(self.manifest))

    def test_legacy_delivery_cannot_overwrite_another_cards_pending_receipt(self):
        alpha = dict(self.item, subject_ref='card:alpha', answer={'text': 'Repair it now'})
        self.respond(self.listing([alpha]))
        answers.stage(self.manifest, alpha)  # retain an undelivered receipt
        self.legacy_reply()  # publishes the loop-audit decision, not card:alpha
        self.assertEqual(read_json(answers.receipt_path(self.manifest, alpha))['phase'], 'pending')
        self.assertIn('pending=1', answers.health(self.manifest))
        self.assertEqual(self.rows()[0]['subject'], 'card:loop-audit')

    def test_legacy_receipt_for_different_card_stays_pending(self):
        self.item.update(subject_ref='card:alpha', answer={'text': 'Repair it now'})
        legacy = dict(self.item)
        self.respond(self.listing())
        answers.stage(self.manifest, self.item)  # retain an undelivered receipt
        self.item.update(subject_ref='card:beta', answer={'text': 'Repair it now', 'response_event_id': 'response'})
        self.respond(self.listing())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(answers.shared_answer(self.manifest, self.item))
        self.assertEqual(self.rows()[0]['subject'], 'card:beta')
        self.assertEqual(read_json(answers.receipt_path(self.manifest, legacy))['phase'], 'pending')
        self.assertIn('pending=1', answers.health(self.manifest))
        self.assertEqual(len(self.reads()), 1)

    def test_legacy_receipt_for_different_recipient_stays_pending(self):
        from coordkit import briefs
        blocker = {'id': 'need-worker', 'kind': 'blocker', 'origin': 'coordkit',
                   'frm': 'worker', 'to': 'lead', 'owner': 'lead', 'subject': 'card:loop-audit',
                   'text': 'Need a decision', 'ts': bus.now().isoformat()}
        bus.configure(self.manifest)
        bus.append([blocker], project=False)
        self.item.update(title=briefs.blocker_title(blocker), answer={'text': 'Repair it now'})
        legacy = dict(self.item)
        self.respond(self.listing())
        answers.stage(self.manifest, self.item)  # retain an undelivered receipt
        self.item.pop('title')
        self.item['answer'] = {'text': 'Repair it now', 'response_event_id': 'response'}
        self.respond(self.listing())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(answers.shared_answer(self.manifest, self.item))
        self.assertEqual(read_json(answers.receipt_path(self.manifest, legacy))['phase'], 'pending')
        self.assertIn('pending=1', answers.health(self.manifest))
        decisions = [r for r in self.rows() if r['kind'] == 'decision']
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]['to'], 'coordinator')

    def test_closed_blocker_proof_does_not_override_another_open_blockers_recipient(self):
        from coordkit import briefs
        self.manifest.obligations.coordinator = 'worker'
        first = {'id': 'first-blocker', 'kind': 'blocker', 'origin': 'coordkit',
                 'frm': 'worker', 'to': 'lead', 'owner': 'lead', 'subject': 'card:loop-audit',
                 'text': 'First issue', 'ts': bus.now().isoformat()}
        second = dict(first, id='second-blocker', frm='other-worker', text='Second issue')
        bus.configure(self.manifest)
        bus.append([first, second], project=False)
        self.item.update(title=briefs.blocker_title(second), answer={'text': 'Repair it now'})
        legacy = dict(self.item)
        self.respond(self.listing())
        answers.stage(self.manifest, self.item)  # retain an undelivered receipt
        self.item.update(title=briefs.blocker_title(first),
                         answer={'text': 'Repair it now', 'response_event_id': 'response'})
        self.respond(self.listing())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(answers.shared_answer(self.manifest, self.item))
        self.assertEqual(read_json(answers.receipt_path(self.manifest, legacy))['phase'], 'pending')
        self.assertIn('pending=1', answers.health(self.manifest))
        decisions = [r for r in self.rows() if r['kind'] == 'decision']
        self.assertEqual(len(decisions), 1)
        self.assertEqual((decisions[0]['to'], decisions[0]['re']), ('worker', 'first-blocker'))

    def test_published_blocker_proof_survives_identically_titled_open_blocker(self):
        from coordkit import briefs
        first = {'id': 'first-blocker', 'kind': 'blocker', 'origin': 'coordkit',
                 'frm': 'worker', 'to': 'lead', 'owner': 'lead', 'subject': 'card:loop-audit',
                 'text': 'Need a decision', 'ts': bus.now().isoformat()}
        second = dict(first, id='second-blocker')
        bus.configure(self.manifest)
        bus.append([first], project=False)
        self.item.update(title=briefs.blocker_title(first), answer={'text': 'Repair it now'})
        self.respond(self.listing())
        answers.stage(self.manifest, self.item)  # retain an undelivered receipt
        self.item['answer'] = {'text': 'Repair it now', 'response_event_id': 'response'}
        self.respond(self.listing())
        original = answers.bus.main
        def publish_then_open_another(*args):
            original(*args)
            bus.append([second], project=False)
            raise OSError('crash after publish, before receipt settlement')
        with patch.object(answers.bus, 'main', side_effect=publish_then_open_another):
            self.consume()
        self.consume()
        self.assertNotIn('WARN', answers.health(self.manifest))
        self.assertEqual(len(self.reads()), 1)
        self.assertEqual([r['id'] for r in bus.open_blockers(self.rows())], ['second-blocker'])
        decisions = [r for r in self.rows() if r['kind'] == 'decision']
        self.assertEqual(len(decisions), 1)
        self.assertEqual((decisions[0]['to'], decisions[0]['re']), ('worker', 'first-blocker'))

    def test_matching_blocker_reply_settles_legacy_receipt_after_blocker_closes(self):
        from coordkit import briefs
        blocker = {'id': 'need-worker', 'kind': 'blocker', 'origin': 'coordkit',
                   'frm': 'worker', 'to': 'lead', 'owner': 'lead', 'subject': 'card:loop-audit',
                   'text': 'Need a decision', 'ts': bus.now().isoformat()}
        bus.configure(self.manifest)
        bus.append([blocker], project=False)
        self.item.update(title=briefs.blocker_title(blocker), answer={'text': 'Repair it now'})
        self.respond(self.listing())
        self.consume()
        self.item['answer'] = {'text': 'Repair it now', 'response_event_id': 'response'}
        self.respond(self.listing())
        self.consume()
        self.assertEqual(bus.open_blockers(self.rows()), [])
        self.assertNotIn('WARN', answers.health(self.manifest))
        self.item['answer'] = {'text': 'Repair it now', 'outcome': 'answered'}
        self.respond(self.listing())
        self.consume()
        self.assertNotIn('WARN', answers.health(self.manifest))
        self.assertEqual(len(self.reads()), 1)

    def test_subjectless_legacy_receipt_settles_with_matching_loop_destination(self):
        from coordkit import loop_answers
        self.item.pop('subject_ref')
        self.item['answer'] = {'text': 'Repair it now'}
        self.respond(self.listing())
        self.consume()
        loop_answers.remember(self.manifest, self.item['ask_id'], 'card:loop-audit', 'audit', 'cycle')
        self.item.update(subject_ref='card:loop-audit', answer={'text': 'Repair it now', 'response_event_id': 'response'})
        self.respond(self.listing())
        self.consume()
        self.assertNotIn('WARN', answers.health(self.manifest))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.reads()), 1)

    def test_late_correlation_settles_legacy_receipt_after_identified_reply_is_read(self):
        from coordkit import loop_answers
        self.item.pop('subject_ref')
        self.item['answer'] = {'text': 'Repair it now'}
        self.respond(self.listing())
        self.consume()
        self.item.update(subject_ref='card:loop-audit', answer={'text': 'Repair it now', 'response_event_id': 'response'})
        self.respond(self.listing())
        self.consume()
        self.assertIn('pending=1', answers.health(self.manifest))
        loop_answers.remember(self.manifest, self.item['ask_id'], 'card:loop-audit', 'audit', 'cycle')
        self.respond(self.listing([]))
        self.consume()
        self.assertNotIn('WARN', answers.health(self.manifest))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.reads()), 1)

    def test_legacy_binding_survives_archived_loop_correlation(self):
        from coordkit import loop_answers
        self.legacy_reply()
        loop_answers.path(self.manifest, self.item['ask_id']).unlink()
        self.respond(self.listing())
        self.consume()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.reads()), 1)

    def test_legacy_append_crash_then_identified_reply_does_not_republish(self):
        from coordkit import loop_answers
        with patch.object(answers, 'record_legacy_delivery', side_effect=OSError('crash after append')):
            pending = self.legacy_reply()
        self.assertTrue(pending.exists())
        self.respond(self.listing())
        self.consume()
        with contextlib.redirect_stderr(io.StringIO()):
            loop_answers.drain(self.manifest)
        self.assertFalse(pending.exists())
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.reads()), 1)

    def test_identified_append_crash_then_legacy_reply_does_not_republish(self):
        self.respond(self.listing())
        original = answers.bus.main
        def append_then_fail(*args):
            original(*args)
            raise OSError('crash after append')
        with patch.object(answers.bus, 'main', side_effect=append_then_fail):
            self.consume()
        self.assertEqual(self.reads(), [])
        self.assertFalse(self.legacy_reply().exists())
        self.assertEqual(len(self.rows()), 1)
        self.consume()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.reads()), 1)

    def test_option_shaped_answer_is_inert_bus_text(self):
        for index, text in enumerate(('--force', '--no-mirror')):
            with self.subTest(text=text):
                self.item['answer'] = {'text': text, 'response_event_id': f'option-{index}'}
                self.respond(self.listing())
                self.consume()
        self.assertEqual({r['text'] for r in self.rows()}, {'--force', '--no-mirror'})
        self.assertEqual(len(self.reads()), 2)

    def test_malformed_ask_does_not_block_later_unread_or_shared_answers(self):
        from coordkit import lifecycle
        from coordkit.anxcli import AnxClient
        for stream in (False, True):
            with self.subTest(stream=stream):
                bad = dict(self.item, ask_id='event:bad/ask')
                self.item['answer'] = {'text': 'Valid answer', 'response_event_id': f'valid-{stream}'}
                if stream:
                    self.respond({'tool': 'anx', 'contains': ['await'],
                                  'json': {'ok': True, 'result': {'answers': [bad, self.item]}}})
                    with contextlib.redirect_stderr(io.StringIO()):
                        result = AnxClient(self.manifest).call('await', '--answers')
                    self.assertEqual(len(result['answers']), 1)
                else:
                    self.respond(self.listing([bad, self.item]))
                    self.consume()
                self.assertIn('invalid=', answers.health(self.manifest))
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(len(self.reads()), 2)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            lifecycle.doctor(self.manifest)
        self.assertIn('DOCTOR unread_answers warn', out.getvalue())

    def test_foreign_ask_in_returned_listing_and_stream_is_untouched(self):
        from coordkit import loop_answers
        from coordkit.anxcli import AnxClient
        foreign = dict(self.item, ask_id='event:foreign', requester_actor_id='actor:somebody-else')
        self.respond(self.listing([foreign]), {'tool': 'anx', 'contains': ['await'],
                     'json': {'ok': True, 'result': {'answers': [foreign]}}})
        self.consume()
        self.assertEqual(AnxClient(self.manifest).call('await', '--answers')['answers'], [])
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.reads(), [])
        self.assertFalse(answers.receipt_path(self.manifest, foreign).exists())
        self.assertEqual(loop_answers.deferred(self.manifest), [])

    def test_explicit_requester_matching_own_principal_is_delivered(self):
        self.item['requester_actor_id'] = 'actor:own-principal'
        self.respond(self.listing(), {'tool': 'anx', 'contains': ['orient'],
                     'json': {'ok': True, 'result': {'principal': {'id': 'own-principal'}}}})
        self.consume()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.reads()), 1)

    def test_dry_run_neither_consumes_nor_acknowledges_pending_answers(self):
        self.respond(self.listing())
        with patch.object(answers.bus, 'main', side_effect=SystemExit(1)):
            self.consume()
        before = {p: p.read_bytes() for p in answers.home(self.manifest).glob('*') if p.is_file()}
        call_count = len(self.calls())
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            reconcile.reconcile(self.manifest, dry_run=True)
        after = {p: p.read_bytes() for p in answers.home(self.manifest).glob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.reads(), [])
        calls = self.calls()[call_count:]
        self.assertFalse(any(c['tool'] == 'anx' and ('--unread' in c['args'] or 'await' in c['args']) for c in calls))

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
                     'ts': bus.now().isoformat()}], project=False)
        self.respond(self.listing())
        self.consume()
        row = next(r for r in self.rows() if r['kind'] == 'decision')
        self.assertEqual(row['to'], 'coordinator')
        self.assertNotIn('re', row)
        self.assertEqual(len(bus.open_blockers(self.rows())), 1)

    def question(self):
        from coordkit import questions
        from coordkit.state import write_json
        self.manifest.escalation.card = 'card:loop-audit'
        record = {'qid': 'q-123abc', 'state': 'escalated', 'created_at': time.time(),
                  'anx_ask_id': self.item['ask_id'], 'asker': 'worker', 'capture': 'modal',
                  'tier': 2, 'guarded': False, 'default': 'Skip the gate', 'cycles': 99,
                  'deadline_at': 0, 'question': 'Need a decision'}
        write_json(questions.home(self.manifest)/'questions.json', {'q-123abc': record})
        return record

    def test_two_identical_response_texts_publish_distinct_decisions(self):
        self.respond(self.listing())
        self.consume()
        self.item['answer'] = {'text': 'Repair it now', 'response_event_id': 'another-response'}
        self.respond(self.listing())
        self.consume()
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(len({r['id'] for r in self.rows()}), 2)
        self.assertEqual(len(self.reads()), 2)

    def test_question_lifecycle_settles_before_ack_even_without_inbox_fallback(self):
        from coordkit import questions
        self.question()
        self.respond(self.listing())
        original = answers.AnxClient.call
        def check(client, *args, **kwargs):
            if args[:2] == ('api', 'call'):
                record = questions.load_all(self.manifest)['q-123abc']
                self.assertIn(record['state'], ('answered', 'closed'))
                self.assertEqual(record['answer'], 'Repair it now')
            return original(client, *args, **kwargs)
        with patch.object(answers.AnxClient, 'call', check):
            self.consume()
        with patch.object(questions, 'deliver', return_value='waiter'):
            questions.ladder(self.manifest, anx_answers={}, anx_known=None)
        self.assertEqual(questions.load_all(self.manifest)['q-123abc']['state'], 'closed')
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.reads()), 1)

    def test_question_sweep_and_unread_consumer_share_one_publication(self):
        from coordkit import questions
        record = self.question()
        self.respond(self.listing())
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            questions.relay_anx_answer(self.manifest, record, self.item['answer'])
        self.consume()
        self.assertEqual(len(self.rows()), 1)
        self.assertIn(questions.load_all(self.manifest)['q-123abc']['state'], ('answered', 'closed'))

    def test_pending_legacy_loop_delivery_clears_after_unread_delivery(self):
        from coordkit import loop_answers
        loop_answers.remember(self.manifest, self.item['ask_id'], 'card:loop-audit', 'audit', 'cycle')
        with patch.object(answers.bus, 'main', side_effect=SystemExit(1)), contextlib.redirect_stderr(io.StringIO()):
            loop_answers.receive(self.manifest, self.item)
        pending = loop_answers.path(self.manifest, self.item['ask_id']).with_suffix('.answer.json')
        self.assertTrue(pending.exists())
        self.respond(self.listing())
        self.consume()
        loop_answers.drain(self.manifest)
        self.assertFalse(pending.exists())
        self.assertEqual(len(self.rows()), 1)

    def test_legacy_string_reply_is_visible_when_exact_ack_id_is_missing(self):
        self.item['answer'] = 'Keep retrying while investigating'
        self.respond(self.listing())
        self.consume()
        self.assertFalse(self.reads())
        self.assertIn('source_read_unconfirmed=1', answers.health(self.manifest))

    def test_concurrent_question_settlement_is_preserved(self):
        from coordkit import questions
        self.question()
        self.respond(self.listing())
        original = answers.bus.main
        def settle_then_return(*args):
            original(*args)
            questions.mark(self.manifest, 'q-123abc', state='closed', closed_at=time.time(),
                           answer='An agent already settled this', answered_by='worker')
        with patch.object(answers.bus, 'main', side_effect=settle_then_return):
            self.consume()
        record = questions.load_all(self.manifest)['q-123abc']
        self.assertEqual(record['answer'], 'An agent already settled this')
        self.assertEqual(record['answered_by'], 'worker')
        self.assertEqual(len(self.reads()), 1)

    def test_loop_answer_provenance_survives_inbox_outage(self):
        from coordkit import loop_answers
        loop_answers.remember(self.manifest, self.item['ask_id'], 'card:loop-audit', 'audit', 'cycle')
        self.respond(self.listing())
        self.consume()
        with patch.object(answers.AnxClient, 'call', side_effect=AssertionError('must use local answer provenance')):
            text = loop_answers.answer_context(self.manifest, self.item['ask_id'])
        self.assertIn('Repair it now', text)

    def test_alternate_shared_reply_identifiers_preserve_correlation(self):
        from coordkit import loop_answers
        from coordkit.anxcli import AnxClient
        loop_answers.remember(self.manifest, self.item['ask_id'], 'card:loop-audit', 'audit', 'cycle')
        self.respond(self.listing())
        self.consume()
        for field in ('event_ref', 'id'):
            item = dict(self.item)
            item[field] = item.pop('ask_id').removeprefix('event:')
            self.respond({'tool': 'anx', 'contains': ['await'], 'json': {'ok': True, 'result': {'answers': [item]}}})
            self.assertEqual(AnxClient(self.manifest).call('await', '--answers')['answers'], [])
        self.assertEqual(len(self.rows()), 1)

    def test_stale_agent_close_cannot_replace_the_operator_settlement(self):
        from coordkit import questions
        stale = self.question()
        self.respond(self.listing())
        self.consume()
        questions.close(self.manifest, stale, 'worker', 'A stale conflicting answer')
        record = questions.load_all(self.manifest)['q-123abc']
        self.assertEqual(record['answer'], 'Repair it now')
        self.assertEqual(record['answered_by'], self.manifest.questions.human_actor)

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
