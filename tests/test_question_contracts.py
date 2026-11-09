"""Question publication must serialize against real answers and preserve new information."""
import fcntl
import io
import json
import os
import threading
from unittest.mock import patch

from coordkit import bus, mcp_server, questions
from coordkit.manifest import ManifestError
from coordkit.tail import read_tail
from tests.test_addressed import AddressedCase, ts


class QuestionContractTests(AddressedCase):
    def setUp(self):
        super().setUp()
        self.manifest.bus.mirror = False
        self.lease('lead-a')

    def post(self, text='Use the cached result?', *flags, sender='worker-ui', recipient='lead-a', topic='result'):
        self.quiet(bus.main, ['post', '--from', sender, '--to', recipient,
                             '--kind', 'ask', '--topic', topic, *flags, text], self.manifest)
        return bus.LAST_POST[0]

    def test_explicit_keys_are_scoped_to_sender_declared_recipient_and_topic(self):
        # Reusing a key must deliver every independent ask, including when only
        # one identity field differs; a retry within each scope still dedupes.
        self.manifest.addressed.agents['lead-b'] = self.manifest.addressed.agents['lead-a']
        self.lease('lead-b')
        originals = []
        with patch('coordkit.adapters.notify') as wake:
            for sender in ('worker-ui', 'worker-other'):
                for recipient in ('lead-a', 'lead-b'):
                    for topic in ('cache', 'build'):
                        scope = dict(sender=sender, recipient=recipient, topic=topic)
                        first = self.post('Use cache?', '--key', 'cache', **scope)
                        self.assertEqual(self.post('Use cache?', '--key', 'cache', **scope)['id'], first['id'])
                        originals.append(first)
            self.assertEqual(wake.call_count, 8)
        self.assertEqual(len({r['id'] for r in originals}), 8)
        self.assertEqual({(r['frm'], r['to'], r['topic']) for r in self.rows()},
                         {(r['frm'], r['to'], r['topic']) for r in originals})
        self.assertEqual(len(self.rows()), 8)
        records = questions.load_all(self.manifest)
        self.assertEqual(len(records), 8)
        self.assertTrue(all(r['state'] == 'open' and r['reasks'] == 1 for r in records.values()),
                        [(r['asker'], r['target'], r['topic'], r['state'], r['reasks']) for r in records.values()])

    def test_explicit_key_retains_declared_role_when_live_member_changes(self):
        first = self.post('Use cache?', '--key', 'cache', recipient='coordinator')
        self.assertEqual((first['role'], first['to']), ('coordinator', 'lead-a'))
        self.lease('coordinator')
        again = self.post('Use cache?', '--key', 'cache', recipient='coordinator')
        self.assertEqual(again['id'], first['id'])
        direct = self.post('Use cache?', '--key', 'cache')
        self.assertNotEqual(direct['id'], first['id'])

    def answer_race(self, harness, recovery=False):
        if harness:
            with patch.object(questions, 'route'):
                record, _ = questions.create(self.manifest, 'worker-ui', 'Use cache?', default='Use cache')
            questions.mark(self.manifest, record['qid'], reasks=2)
            qid = record['qid']
        else:
            qid = self.post('Use cache?', '--default', 'Use cache')['id']
        if recovery:
            with patch.object(questions, 'close', side_effect=OSError('interrupted completion')):
                with self.assertRaises(OSError):
                    questions.apply_default(self.manifest, questions.load_all(self.manifest)[qid], self.now + 1)
            self.assertTrue(questions.load_all(self.manifest)[qid]['auto_default_applied'])
        stale = self.rows()
        real = dict(id='real-answer', ts=ts(self.now), frm='lead-a', to='worker-ui',
                    kind='answer', text='Fetch fresh data', re=qid)
        appended, errors, writers = threading.Event(), [], []
        append, scan = bus.append, questions.settlement_rows
        locked_checks = []

        def writer():
            try:
                def committed():
                    appended.set()  # fsynced; its projection now waits for the sweep's question lock
                    questions.track_bus_reply(self.manifest, real)
                append([real], project=False, on_commit=committed)
            except BaseException as exc:
                errors.append(exc)
                appended.set()

        def interleave(rows, **kwargs):
            if any(r.get('auto_default') for r in rows):
                thread = threading.Thread(target=writer, daemon=True)
                writers.append(thread)
                thread.start()
                self.assertTrue(appended.wait(10), 'answer writer did not commit')
            return append(rows, **kwargs)

        def inspect_lock(manifest, stream, record):
            if stream is not None:
                # Rechecking immediately before append is insufficient unless writers are excluded.
                with manifest.paths.bus_log.open('rb') as other:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked_checks.append(True)
            return scan(manifest, stream, record)

        try:
            with patch.object(bus, 'append', side_effect=interleave), \
                    patch.object(questions, 'settlement_rows', side_effect=inspect_lock), \
                    patch.object(questions, 'deliver', return_value='waiter'):
                if harness:
                    questions.apply_default(self.manifest, questions.load_all(self.manifest)[qid],
                                            self.now + 3601, stale)
                else:
                    questions.reconcile_defaults(self.manifest, stale, self.now + 3601)
        finally:
            for thread in writers:
                thread.join(10)
        self.assertTrue(writers)
        self.assertTrue(all(not t.is_alive() for t in writers))
        self.assertEqual(errors, [])
        self.assertTrue(locked_checks)
        self.assertEqual(sum(bool(r.get('auto_default')) for r in self.rows()), int(recovery))
        stored = questions.load_all(self.manifest)[qid]
        self.assertFalse(stored.get('auto_default_applied'))
        self.assertEqual((stored['state'], stored['answered_by'], stored['answer']),
                         ('closed', 'lead-a', 'Fetch fresh data'))

    def test_real_bus_answer_wins_while_its_projection_waits_for_the_question_lock(self):
        self.answer_race(False)

    def test_real_ladder_answer_wins_and_uses_normal_delivery(self):
        self.answer_race(True)

    def test_real_answer_wins_while_recovering_an_existing_default_receipt(self):
        self.answer_race(True, recovery=True)

    def test_ref_change_reposts_and_wakes_instead_of_returning_the_old_receipt(self):
        ref = self.root/'brief.txt'
        ref.write_text('Initial evidence')
        with patch('coordkit.adapters.notify') as wake:
            first = self.post('Use cache?', '--ref', str(ref), '--key', 'cache', '--default', 'Use cache')
            self.assertEqual(self.post('Use cache?', '--ref', str(ref), '--key', 'cache',
                                       '--default', 'Use cache')['id'], first['id'])
            ref.write_text('New evidence: the cached result is stale')
            second = self.post('Use cache?', '--ref', str(ref), '--key', 'cache', '--default', 'Use cache')
            other = self.root/'other-brief.txt'
            other.write_text(ref.read_text())
            third = self.post('Use cache?', '--ref', str(other), '--key', 'cache', '--default', 'Use cache')
            fourth = self.post('Use cache?', '--key', 'cache', '--default', 'Use cache')
        self.assertEqual(wake.call_count, 4)
        self.assertEqual(len({r['id'] for r in (first, second, third, fourth)}), 4)
        self.assertNotEqual(first['ask_ref'], second['ask_ref'])
        records = questions.load_all(self.manifest)
        self.assertEqual([r['qid'] for r in records.values() if r['state'] == 'open'], [fourth['id']])
        # An older projection did not capture ref/priority evidence; renew it once conservatively.
        questions.update(self.manifest, lambda records: records[fourth['id']].pop('ask_ref'))
        fifth = self.post('Use cache?', '--key', 'cache', '--default', 'Use cache')
        self.assertNotEqual(fifth['id'], fourth['id'])
        self.assertEqual(self.post('Use cache?', '--key', 'cache', '--default', 'Use cache')['id'], fifth['id'])

    def test_priority_promotion_is_new_information_with_or_without_an_explicit_key(self):
        for text, key in [('Use cached result?', []), ('Use fresh result?', ['--key', 'fresh'])]:
            with self.subTest(key=key), patch('coordkit.adapters.notify') as wake:
                first = self.post(text, *key)
                self.assertEqual(self.post(text, *key, '--priority', 'normal')['id'], first['id'])
                second = self.post(text, *key, '--priority', 'urgent')
                self.assertNotEqual(first['id'], second['id'])
                self.assertEqual(self.post(text, *key, '--priority', 'urgent')['id'], second['id'])
                self.assertEqual(wake.call_count, 2)
                self.assertEqual(questions.load_all(self.manifest)[first['id']]['closed_by'], 'superseded')

    def test_numeric_decisions_stay_distinct_while_marked_metadata_normalizes(self):
        for a, b in [('Use a 30s timeout?', 'Use a 60s timeout?'),
                     ('Use a 30-second timeout?', 'Use a 60-second timeout?'),
                     ('Use a 30m timeout?', 'Use a 60m timeout?'),
                     ('Set the limit to 30?', 'Set the limit to 60?')]:
            with self.subTest(text=a):
                self.assertNotEqual(questions.fingerprint('worker', a), questions.fingerprint('worker', b))
                self.assertNotEqual(self.post(a)['id'], self.post(b)['id'])
        a = 'Retry #123 FEATURE_GATE_V1 at 2026-11-09T12:30:01Z q-abcdef attempt 1 age=35m?'
        b = 'RETRY #123 FEATURE_GATE_V1 at 2026-11-10T13:45:59Z q-123abc attempt 22 age=90m?'
        self.assertEqual(questions.normalize(a), questions.normalize(b))
        self.assertEqual(questions.normalize(a.replace('attempt 1', 'attempt=1')),
                         questions.normalize(b.replace('attempt 22', 'attempt:22')))
        self.assertNotEqual(questions.normalize(a), questions.normalize(a.replace('#123', '#124')))
        self.assertNotEqual(questions.normalize(a), questions.normalize(a.replace('GATE_V1', 'GATE_V2')))

    def test_kill_switch_freezes_pending_bus_and_ladder_settlement_then_resumes(self):
        qid = self.post('Use cache?', '--default', 'Use cache')['id']
        with patch.object(questions, 'route'):
            harness, _ = questions.create(self.manifest, 'worker-ui', 'Use fresh data?', default='Fetch fresh')
        questions.mark(self.manifest, harness['qid'], reasks=2)
        before = questions.load_all(self.manifest)
        with patch.dict(os.environ, COORDKIT_QUESTION_GUARD='off'):
            questions.reconcile_defaults(self.manifest, self.rows(), self.now + 7200)
            self.assertFalse(questions.apply_default(self.manifest, before[harness['qid']], self.now + 7200))
            self.assertEqual(questions.ladder(self.manifest, self.now + 7200), [])
            self.assertEqual(questions.sweep(self.manifest, self.now + 7200, out=lambda _: None), [])
            self.assertEqual(questions.load_all(self.manifest), before)
        questions.reconcile_defaults(self.manifest, self.rows(), self.now + 7200)
        self.assertTrue(questions.load_all(self.manifest)[qid]['auto_default_applied'])

    def test_kill_switch_is_checked_again_under_the_bus_append_lock(self):
        qid = self.post('Use cache?', '--default', 'Use cache')['id']
        append = bus.append
        def stop(rows, **kwargs):
            with patch.dict(os.environ, COORDKIT_QUESTION_GUARD='off'):
                return append(rows, **kwargs)
        with patch.object(bus, 'append', side_effect=stop):
            questions.reconcile_defaults(self.manifest, self.rows(), self.now + 7200)
        self.assertEqual(questions.load_all(self.manifest)[qid]['state'], 'open')
        self.assertFalse(any(r.get('auto_default') for r in self.rows()))

    def test_fallback_never_uses_an_unbounded_read_or_readline_on_a_giant_row(self):
        class Bounded(io.BytesIO):
            read_bytes = 0
            def read(self, size=-1):
                self.assert_size(size)
                data = super().read(size)
                self.read_bytes += len(data)
                return data
            def assert_size(self, size):
                if not 0 <= size <= 128:
                    raise AssertionError('unbounded read')
            def readline(self, *args):
                raise AssertionError('readline may scan an entire giant row')
        stream = Bounded(b'x' * 4096)
        self.assertEqual(read_tail(stream, 128), (4096, b''))
        self.assertLessEqual(stream.read_bytes, 129)
        row = dict(id='tail', ts=ts(self.now), frm='worker-ui', to='lead-a', kind='ask', text='Fresh?')
        stream = Bounded(b'x' * 4096 + b'\n' + json.dumps(row).encode() + b'\n{"partial":')
        with patch.object(questions, 'MAX_BUS_BYTES', 128):
            # A complete row larger than the window is also omitted, never partially decoded.
            self.assertEqual(questions.recent_rows(self.manifest, stream=stream), [])
        self.assertLessEqual(stream.read_bytes, 129)
        small = json.dumps(dict(id='tail', kind='fyi')).encode()
        stream = Bounded(b'x' * 4096 + b'\n' + small + b'\n{"partial":')
        with patch.object(questions, 'MAX_BUS_BYTES', 128):
            rows = questions.recent_rows(self.manifest, stream=stream)
        self.assertEqual([row['id'] for row in rows], ['tail'])
        self.assertLessEqual(stream.read_bytes, 129)
        self.assertEqual(read_tail(io.BytesIO(b'first\nsecond\n'), 7), (6, b'second\n'))

    def test_sweep_uses_one_bounded_tail_instead_of_reading_the_whole_bus(self):
        self.post()
        with self.manifest.paths.bus_log.open('ab') as stream:
            stream.write(b'x' * (questions.MAX_BUS_BYTES * 2))
        with patch.object(bus, 'read', side_effect=AssertionError('unbounded bus read')), \
                patch.object(questions, 'scan_transcripts', return_value=[]), \
                patch.object(questions, 'scan_notifications', return_value=[]), \
                patch.object(questions, 'recent_rows', wraps=questions.recent_rows) as recent:
            questions.sweep(self.manifest, self.now + 1, out=lambda _: None)
        # The path wrapper plus its open-stream call make up one physical read.
        self.assertEqual(recent.call_count, 2)

    def fill_history(self):
        # Enough complete small records to evict the ask from the tail and require
        # multiple bounded passes, without relying on wall-clock time or CLI cost.
        with self.manifest.paths.bus_log.open('ab') as stream:
            for i in range(800):
                stream.write(json.dumps(dict(id=f'fyi-{i}', ts=ts(self.now), frm='worker-ui', to='lead-a', kind='fyi', text='x' * 1000)).encode() + b'\n')

    def test_harness_answers_survive_traffic_before_the_next_sweep(self):
        for policy in ({}, {'default': 'Use cache', 'no_auto_default': True}):
            for reference in ('qid', 'bus_id', 'bus_ids'):
                with self.subTest(policy=policy, reference=reference):
                    with patch.object(questions, 'route'):
                        record, _ = questions.create(self.manifest, 'worker-ui', 'Use cache?',
                                                     session_id=str((policy, reference)), **policy)
                    qid = record['qid']
                    alias = qid if reference == 'qid' else 'hop-' + qid
                    binding = {} if reference == 'qid' else {reference: [alias] if reference == 'bus_ids' else alias}
                    questions.mark(self.manifest, qid, **binding, anx_ask_id='pending-ask')
                    self.quiet(bus.main, ['post', '--from', 'lead-a', '--to', 'worker-ui',
                                         '--kind', 'answer', '--re', alias, 'Fetch fresh data'], self.manifest)
                    answer = bus.LAST_POST[0]
                    self.fill_history()  # >256 KiB after the answer, with no intervening sweep
                    rows = questions.recent_rows(self.manifest)
                    self.assertFalse(any(r['id'] == answer['id'] for r in rows))
                    with patch.object(questions, 'deliver', return_value='waiter') as deliver, \
                            patch.object(questions, 'withdraw_anx') as withdraw, \
                            patch.object(questions, 'tier2') as escalate:
                        self.quiet(questions.ladder, self.manifest, self.now + 7200, rows=rows)
                    stored = questions.load_all(self.manifest)[qid]
                    self.assertEqual((stored['state'], stored['answer_id'], stored['answer']),
                                     ('closed', answer['id'], 'Fetch fresh data'))
                    deliver.assert_called_once()
                    withdraw.assert_called_once()
                    escalate.assert_not_called()
                    self.assertFalse(stored.get('auto_default_applied'))

    def test_cached_answer_preserves_physical_first_reply_order(self):
        for ago in (0, -60):  # equal timestamps and an incumbent with a clock ahead
            with self.subTest(ago=ago), patch.object(questions, 'route'):
                record, _ = questions.create(self.manifest, 'worker-ui', 'Use cache?', session_id=str(ago))
            qid = record['qid']
            earlier = self.incumbent_row('incumbent-' + qid, 'lead-a', 'worker-ui', 'answer',
                                         'First answer', ago=ago, re=qid)
            self.quiet(bus.main, ['post', '--from', 'lead-a', '--to', 'worker-ui', '--kind', 'answer',
                                 '--re', qid, 'Later answer'], self.manifest)
            stored = questions.load_all(self.manifest)[qid]
            self.assertEqual(stored['bus_answer']['text'], 'Later answer')
            self.assertEqual(questions.replies(self.manifest, self.rows(), stored)['id'], earlier['id'])
            with patch.object(questions, 'deliver', return_value='waiter'):
                self.quiet(questions.ladder, self.manifest, self.now + 1, rows=self.rows())
            self.assertEqual(questions.load_all(self.manifest)[qid]['answer_id'], earlier['id'])

    def test_evicted_cached_reply_precedes_later_answers(self):
        record = dict(qid='q-test', asker='worker-ui')
        saved = dict(id='first', ts=ts(self.now), seq=5, frm='lead-a', re='q-test', kind='answer', text='First')
        record['bus_answer'] = saved
        for newer in (dict(saved, id='later', seq=6, ts=ts(self.now - 60)),
                      dict(saved, id='incumbent', ts=ts(self.now + 60))):
            if newer['id'] == 'incumbent':
                newer.pop('seq')
            with self.subTest(newer=newer['id']):
                self.assertEqual(questions.replies(self.manifest, [newer], record)['id'], 'first')
        older = dict(saved, id='older', seq=4, ts=ts(self.now + 60))
        self.assertEqual(questions.replies(self.manifest, [older], record)['id'], 'older')

    def test_legacy_tail_identity_matches_full_reader_and_settlement(self):
        # A nonzero byte tail and a record cap must both retain physical line
        # numbers, counting malformed and blank lines as the full reader does.
        path = self.manifest.paths.bus_log
        path.parent.mkdir(parents=True, exist_ok=True)
        old = json.dumps(dict(id='old', kind='fyi', text='x' * 1000)).encode() + b'\n'
        legacy = dict(ts=ts(self.now), frm='worker-ui', to='lead-a', kind='ask',
                      text='Use cache?', default='Use cache')
        path.write_bytes(old * 300 + b'\nnot-json\n' + json.dumps(legacy).encode() + b'\n')
        canonical = self.quiet(self.rows)[0][-1]['id']
        self.assertEqual(canonical, 'legacy-L302')
        for cap in (questions.MAX_RECORDS, 1):
            with self.subTest(record_cap=cap), patch.object(questions, 'MAX_RECORDS', cap):
                start, rows = self.quiet(questions.recent_rows, self.manifest, coverage=True)[0]
                self.assertGreater(start, 0)
                self.assertEqual(rows[-1]['id'], canonical)
        rows = self.quiet(questions.recent_rows, self.manifest)[0]
        self.quiet(questions.capture_bus_asks, self.manifest, rows, self.now)
        self.assertEqual(set(questions.load_all(self.manifest)), {canonical})
        self.incumbent_row('reply', 'lead-a', 'worker-ui', 'answer', 'Fetch fresh', ago=0, re=canonical)
        previous_start, _ = self.quiet(questions.recent_rows, self.manifest, coverage=True)[0]
        for _ in range(20):
            self.incumbent_row('newer', 'worker-ui', 'lead-a', 'fyi', 'x' * 1000)
            # Every append shifts the byte tail, but never the physical identity.
            start_after, shifted = self.quiet(questions.recent_rows, self.manifest, coverage=True)[0]
            self.assertGreater(start_after, previous_start)
            previous_start = start_after
            self.assertEqual(next(r['id'] for r in shifted if r['kind'] == 'ask'), canonical)
            self.assertEqual(next(r['id'] for r in self.quiet(self.rows)[0] if r['kind'] == 'ask'), canonical)
            self.quiet(questions.capture_bus_asks, self.manifest, shifted, self.now)
            self.assertEqual(set(questions.load_all(self.manifest)), {canonical})
        rows = self.quiet(questions.recent_rows, self.manifest)[0]
        self.assertEqual(next(r['id'] for r in rows if r['kind'] == 'ask'), canonical)
        record = questions.load_all(self.manifest)[canonical]
        with path.open('rb') as stream:
            fresh, complete = self.quiet(questions.settlement_rows, self.manifest, stream, record)[0]
            self.assertFalse(complete)
            fresh, complete = self.quiet(questions.settlement_rows, self.manifest, stream, record)[0]
        self.assertTrue(complete)
        self.assertTrue(record['settlement_scan']['original_seen'])
        self.assertEqual(next(r['id'] for r in fresh if r['kind'] == 'ask'), canonical)
        self.quiet(questions.reconcile_defaults, self.manifest, rows, self.now + 7200)
        record = questions.load_all(self.manifest)[canonical]
        self.assertEqual((record['state'], record['answer_id']), ('closed', 'reply'))
        self.assertFalse(record.get('auto_default_applied'))

    def test_unknown_physical_offset_skips_legacy_rows_but_preserves_authored_ids(self):
        class Bounded(io.BytesIO):
            read_bytes = 0
            def read(self, size=-1):
                if not 0 <= size <= questions.MAX_BUS_BYTES:
                    raise AssertionError('unbounded read')
                data = super().read(size)
                self.read_bytes += len(data)
                return data
        legacy = dict(ts=ts(self.now), frm='worker-ui', to='lead-a', kind='ask', text='Use cache?')
        authored = dict(legacy, id='legacy-L0', text='Use fresh?')
        data = b'x' * (questions.MAX_BUS_BYTES * 3) + b'\n' + b'\n'.join(
            json.dumps(row).encode() for row in (legacy, authored, dict(legacy, id=''),
                                                dict(legacy, id=17))) + b'\n'
        stream = Bounded(data)
        start, rows = questions.recent_rows(self.manifest, stream=stream, coverage=True)
        self.assertGreater(start, questions.MAX_BUS_BYTES)
        self.assertLessEqual(stream.read_bytes, questions.MAX_BUS_BYTES + 1)
        self.assertEqual([r['id'] for r in rows], ['legacy-L0'])
        questions.capture_bus_asks(self.manifest, rows, self.now)
        self.assertEqual(set(questions.load_all(self.manifest)), {'legacy-L0'})

    def catch_up(self, qid, at):
        offsets = []
        for _ in range(10):
            questions.reconcile_defaults(self.manifest, questions.recent_rows(self.manifest), at)
            record = questions.load_all(self.manifest)[qid]
            offsets.append(record.get('settlement_scan', {}).get('offset', 0))
            if record['state'] == 'closed':
                return record, offsets
        self.fail('bounded settlement never caught up')

    def test_old_ask_defaults_after_age_or_two_reasks_with_durable_progress(self):
        for reasks in (0, 2):
            with self.subTest(reasks=reasks):
                qid = self.post(f'Use cache variant {reasks}?', '--default', 'Use cache')['id']
                questions.mark(self.manifest, qid, reasks=reasks)
                self.fill_history()
                self.assertFalse(any(r.get('id') == qid for r in questions.recent_rows(self.manifest)))
                # Rebuild the unrelated sequence hint from the verified question
                # prefix as well; deleting it must not cause permanent deferral.
                (bus.STATE/'append.offset.json').unlink(missing_ok=True)
                record, offsets = self.catch_up(qid, self.now + (7200 if reasks == 0 else 1))
                self.assertTrue(record['auto_default_applied'])
                self.assertGreater(len(offsets), 1)
                self.assertEqual(offsets, sorted(offsets))
                self.assertLessEqual(max(b-a for a, b in zip([0]+offsets, offsets)), questions.MAX_BUS_BYTES)
                self.assertEqual(sum(r.get('re') == qid and r.get('auto_default', False) for r in self.rows()), 1)
                rows = self.rows()
                self.assertEqual(rows[-1]['seq'], len(rows)-1)

    def test_answer_older_than_tail_wins_without_its_projection(self):
        qid = self.post('Use cache?', '--default', 'Use cache')['id']
        self.fill_history()
        self.incumbent_row('old-answer', 'lead-a', 'worker-ui', 'answer', 'Use fresh data', ago=0, re=qid)
        self.fill_history()
        self.assertFalse(any(r.get('id') == 'old-answer' for r in questions.recent_rows(self.manifest)))
        record, offsets = self.catch_up(qid, self.now + 7200)
        self.assertGreater(len(offsets), 1)
        self.assertEqual((record['answered_by'], record['answer_id']), ('lead-a', 'old-answer'))
        self.assertFalse(any(r.get('auto_default') for r in self.rows()))

    def test_answer_after_checkpoint_wins_on_a_later_sweep(self):
        qid = self.post('Use cache?', '--default', 'Use cache')['id']
        questions.reconcile_defaults(self.manifest, self.rows(), self.now + 1)
        checkpoint = questions.load_all(self.manifest)[qid]['settlement_scan']
        self.assertEqual(checkpoint['no_answer_through'], checkpoint['offset'])
        self.incumbent_row('later-answer', 'lead-a', 'worker-ui', 'answer', 'Fetch fresh', ago=0, re=qid)
        self.fill_history()
        record, _ = self.catch_up(qid, self.now + 7200)
        self.assertEqual(record['answer_id'], 'later-answer')
        self.assertFalse(record.get('auto_default_applied'))

    def test_replaced_or_rewritten_bus_invalidates_the_no_answer_proof(self):
        for replace in (False, True):
            with self.subTest(replace=replace):
                qid = self.post(f'Use cache {replace}?', '--default', 'Use cache')['id']
                questions.reconcile_defaults(self.manifest, self.rows(), self.now + 1)
                path = self.manifest.paths.bus_log
                raw = path.read_bytes()
                answer = dict(id='replacement-answer', ts=ts(self.now), frm='lead-a', to='worker-ui',
                              kind='answer', text='Fresh', re=qid)
                if replace:
                    path.unlink()
                path.write_bytes(json.dumps(answer).encode() + b'\n' + raw)
                record, _ = self.catch_up(qid, self.now + 7200)
                self.assertEqual(record['answer_id'], 'replacement-answer')

    def test_oversized_store_fails_closed_and_preserves_its_bytes(self):
        self.post()
        path = questions.home(self.manifest)/'questions.json'
        raw = b' ' * 1025
        path.write_bytes(raw)
        with patch.object(questions, 'MAX_STORE_BYTES', 1024):
            with self.assertRaisesRegex(ManifestError, 'question_store_limit'):
                questions.load_all(self.manifest)
            with self.assertRaisesRegex(ManifestError, 'question_store_limit'):
                questions.update(self.manifest, lambda records: records.clear())
        self.assertEqual(path.read_bytes(), raw)

    def test_record_and_write_caps_never_drop_existing_questions(self):
        self.post()
        path = questions.home(self.manifest)/'questions.json'
        original = path.read_bytes()
        def add(records):
            old = next(iter(records.values()))
            records['another'] = dict(old, qid='another')
        with patch.object(questions, 'MAX_RECORDS', 1):
            with self.assertRaisesRegex(ManifestError, 'question_store_limit'):
                questions.update(self.manifest, add)
        self.assertEqual(path.read_bytes(), original)
        with patch.object(questions, 'MAX_STORE_BYTES', len(original) + 100):
            with self.assertRaisesRegex(ManifestError, 'question_store_limit'):
                questions.update(self.manifest, lambda records: next(iter(records.values())).update(default='x' * 4096))
        self.assertEqual(path.read_bytes(), original)
        records = questions.load_all(self.manifest)
        add(records)
        path.write_text(json.dumps(records))
        oversized = path.read_bytes()
        with patch.object(questions, 'MAX_RECORDS', 1):
            with self.assertRaisesRegex(ManifestError, 'question_store_limit'):
                questions.load_all(self.manifest)
        self.assertEqual(path.read_bytes(), oversized)


    def test_default_cannot_turn_checkpoint_recovery_into_an_unbounded_bus_scan(self):
        path = self.manifest.paths.bus_log
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(json.dumps(dict(kind='fyi', text='x' * questions.MAX_BUS_BYTES)).encode() + b'\n')
        qid = self.post('Use cache?', '--default', 'Use cache')['id']
        (path.parent/'state'/'append.offset.json').unlink()
        before = path.read_bytes()
        questions.reconcile_defaults(self.manifest, questions.recent_rows(self.manifest), self.now + 7200)
        self.assertEqual(path.read_bytes(), before)
        stored = questions.load_all(self.manifest)[qid]
        self.assertEqual(stored['state'], 'open')
        self.assertIn('row exceeds settlement byte limit', stored['default_deferred'])

    def test_clock_choices_remain_distinct_for_bus_and_harness_questions(self):
        for suffix in ('', 'Z'):
            a, b = f'Deploy at 12:30{suffix}?', f'Deploy at 14:30{suffix}?'
            self.assertNotEqual(questions.fingerprint('worker', a), questions.fingerprint('worker', b))
            self.assertNotEqual(self.post(a)['id'], self.post(b)['id'])

    def test_unprovable_history_reports_reason_once_in_sweep_and_digest(self):
        from coordkit import addressed
        qid = self.post('Use cache?', '--default', 'Use cache')['id']
        with self.manifest.paths.bus_log.open('ab') as stream:
            stream.write(json.dumps(dict(kind='fyi', text='x' * questions.MAX_BUS_BYTES)).encode() + b'\n')
        with patch.object(questions, 'scan_transcripts', return_value=[]), \
                patch.object(questions, 'scan_notifications', return_value=[]):
            first, second = [], []
            questions.sweep(self.manifest, self.now + 7200, out=first.append)
            questions.sweep(self.manifest, self.now + 7201, out=second.append)
        self.assertTrue(any(qid in line and 'QUESTION_DEFERRED' in line and 'checked through byte' in line for line in first))
        self.assertFalse(any('QUESTION_DEFERRED' in line for line in second))
        state = {}
        self.quiet(addressed.flush_lead_digest, self.manifest, state, self.now + 7202)
        self.assertIn(qid + ': default deferred: bus row exceeds settlement byte limit', self.digest_text())
        self.quiet(addressed.flush_lead_digest, self.manifest, state, self.now + 10803)
        self.assertEqual(self.digest_text().count('default deferred:'), 1)
        projected = mcp_server.question_rows(self.manifest, {}, {}, self.now + 10803)
        self.assertIn('row exceeds settlement byte limit', projected['questions'][0]['default_deferred'])

    def test_failed_digest_does_not_consume_the_deferral_notice(self):
        from coordkit import addressed
        qid = self.post('Use cache?', '--default', 'Use cache')['id']
        self.manifest.paths.bus_log.write_bytes(b'x' * (questions.MAX_BUS_BYTES + 1))
        with patch.object(bus, 'main', side_effect=OSError('publish unavailable')):
            with self.assertRaises(OSError):
                addressed.flush_lead_digest(self.manifest, {}, self.now + 7200, out=lambda _: None)
        record = questions.load_all(self.manifest)[qid]
        self.assertIn('default_deferred', record)
        self.assertNotIn('default_deferred_digested', record)
