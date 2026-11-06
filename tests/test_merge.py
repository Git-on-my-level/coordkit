import contextlib
import io
import json
from unittest.mock import patch
from coordkit import merge
from tests.support import FakeCase


class MergeTests(FakeCase):
    def setUp(self):
        super().setUp()
        self.manifest.paths.bus_log.parent.mkdir(parents=True)
        self.manifest.paths.bus_log.write_text('')
        self.view={'state':'OPEN','mergeStateStatus':'CLEAN','headRefOid':'a'*40,
                   'statusCheckRollup':[{'name':'unit','status':'COMPLETED','conclusion':'SUCCESS'}]}

    def test_hold_release_authority(self):
        hold={'frm':'w','kind':'hold','prs':['123'],'id':'h'}
        self.assertTrue(merge.holds_on('123',[hold,{'frm':'other','kind':'unhold','prs':['123']}]))
        self.assertFalse(merge.holds_on('123',[hold,{'frm':'w','kind':'unhold','prs':['123']}]))
        self.assertFalse(merge.holds_on('123',[hold,{'frm':'lead','kind':'ruling','text':'merge #123'}]))
        self.assertTrue(merge.holds_on('123',[hold,{'frm':'lead','kind':'fyi','text':'#123 is listed. Merge main into branch.'}]))


class ReviewGateTests(FakeCase):
    """A BLOCK must be visible to merge tools; merges need SHIP at the head."""
    def setUp(self):
        super().setUp()
        self.manifest.paths.bus_log.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.paths.bus_log.write_text('')
        self.manifest.review.ledger = str(self.root/'review-ledger.jsonl')
        self.head = 'c'*40

    def record(self, verdict, head=None, label='ledger-12345', live=None):
        from coordkit import review
        out = self.root/f'rresult-{verdict}-{(head or self.head)[:4]}.out'
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                patch.object(review, 'live_head', return_value=live or head or self.head):
            run = review.start(self.manifest, 12345, head or self.head, str(out), label)
            out.write_text(f'findings...\nVERDICT: {verdict}\n')  # the run writes its result after starting
            return review.record(self.manifest, 12345, head or self.head, str(out), label, run=run, rc=0)

    def bus(self):
        return [json.loads(l) for l in self.manifest.paths.bus_log.read_text().splitlines()]

    def test_block_posts_a_structured_hold_and_ship_releases_it(self):
        self.assertEqual(self.record('BLOCK'), 'BLOCK')
        self.assertTrue(merge.holds_on('12345', self.bus()))
        hold = self.bus()[-1]
        self.assertEqual((hold['kind'], hold['frm'], hold['prs']), ('hold', 'ledger-12345', ['12345']))
        self.record('SHIP', head='d'*40)
        self.assertFalse(merge.holds_on('12345', self.bus()))
        ledger = [json.loads(l) for l in (self.root/'review-ledger.jsonl').read_text().splitlines()]
        self.assertEqual([(r['event'], r['verdict'] if r['event'] == 'end' else None) for r in ledger],
                         [('start', None), ('end', 'BLOCK'), ('start', None), ('end', 'SHIP')])

    def test_missing_verdict_holds(self):
        from coordkit import review
        out = self.root/'empty.out'; out.write_text('no verdict line')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(review.record(self.manifest, 12345, self.head, str(out), 'ledger-x'), 'NO-VERDICT')
        self.assertTrue(merge.holds_on('12345', self.bus()))


    def test_incumbent_ledger_rows_are_read(self):
        ledger = self.root/'review-ledger.jsonl'
        ledger.write_text('\n'.join([
            json.dumps({"repo":"example/project","pr":12347,"round":2,"head":"cccccccccccccccccccccccccccccccccccccccc","event":"start"}),
            '{partial',
            json.dumps({"repo":"example/project","pr":12347,"round":2,"head":"cccccccccccccccccccccccccccccccccccccccc","event":"end","rc":0,"verdict":"SHIP"})]) + '\n')
        from coordkit import review
        self.assertEqual(review.latest(self.manifest, 12347)['verdict'], 'SHIP')

    def test_ship_without_evidence_is_refused_and_stale_ship_keeps_the_hold(self):
        from coordkit import review
        from coordkit.manifest import ManifestError
        self.record('BLOCK')
        with self.assertRaises(ManifestError):
            review.record(self.manifest, 12345, self.head, None, 'ledger-12345', verdict='SHIP')
        # A SHIP for an older head never releases the hold on the newer, unreviewed head.
        self.record('SHIP', head='d'*40, live='f'*40)
        self.assertTrue(merge.holds_on('12345', self.bus()))

    def test_scan_flags_a_started_review_that_never_ended(self):
        from coordkit import review
        ledger = self.root/'review-ledger.jsonl'
        old = '2000-01-01T10:00:00+00:00'
        ledger.write_text('\n'.join(json.dumps(r) for r in [
            {"pr":12345,"round":1,"head":"c"*40,"label":"reviewer-a","event":"start","ts":old,"out":"rresult.out"},
            {"pr":12347,"round":2,"head":"d"*40,"label":"reviewer-b","event":"start","ts":old},
            {"pr":12347,"round":2,"head":"d"*40,"label":"reviewer-b","event":"end","ts":old,"verdict":"SHIP"}]) + '\n')
        from coordkit.process import parse_ts
        found = review.stale(self.manifest, at=parse_ts(old).timestamp() + 2*3600)
        self.assertEqual([r['pr'] for r in found], [12345])

    def test_ship_evidence_is_bound_to_its_pr_and_head(self):
        from coordkit import review
        from coordkit.manifest import ManifestError
        out = self.root/'ship-a.out'
        with contextlib.redirect_stdout(io.StringIO()):
            review.start(self.manifest, 12345, 'a'*40, str(out), 'ledger-12345')
        out.write_text('VERDICT: SHIP\n')
        with self.assertRaises(ManifestError):  # head A's SHIP report reused for head B
            review.record(self.manifest, 12345, 'b'*40, str(out), 'ledger-12345')
        with self.assertRaises(ManifestError):  # another PR
            review.record(self.manifest, 12346, 'a'*40, str(out), 'ledger-12345')

    def test_bare_verdict_lines_parse_and_unledgered_blocks_are_found(self):
        from coordkit import review
        self.assertEqual(review.verdict_from('## Verdict\n\n**BLOCK**\n'), 'BLOCK')
        self.assertEqual(review.verdict_from('VERDICT: FIX-FIRST\nlater\nVERDICT: SHIP'), 'SHIP')
        self.assertIsNone(review.verdict_from('we must not BLOCK the train on this'))
        loose = self.root/'handoff/review-a'; loose.mkdir(parents=True)
        (loose/'rresult.out').write_text('P1 x2\n\n**BLOCK**\n')
        (loose/'rresult-ok.out').write_text('VERDICT: SHIP\n')
        self.manifest.review.result_globs = [str(self.root/'handoff/**/rresult*.out')]
        self.assertEqual([f['verdict'] for f in review.unledgered(self.manifest)], ['BLOCK'])

    def test_stale_or_failed_ship_is_refused(self):
        from coordkit import review
        from coordkit.manifest import ManifestError
        out = self.root/'rresult.out'
        out.write_text('VERDICT: SHIP\n')  # left over from a review of an earlier head
        with contextlib.redirect_stdout(io.StringIO()):
            run = review.start(self.manifest, 12345, self.head, str(out), 'ledger-12345')
        with self.assertRaises(ManifestError):  # the rerun crashed before rewriting the file
            review.record(self.manifest, 12345, self.head, str(out), 'ledger-12345', run=run, rc=0)
        out.write_text('VERDICT: SHIP\n')
        with self.assertRaises(ManifestError):  # rc not given is not success
            review.record(self.manifest, 12345, self.head, str(out), 'ledger-12345', run=run)
        with self.assertRaises(ManifestError):
            review.record(self.manifest, 12345, self.head, str(out), 'ledger-12345', run=run, rc=1)

    def test_started_block_without_an_end_is_flagged_at_once(self):
        from coordkit import review
        out = self.root/'handoff/x/rresult.out'; out.parent.mkdir(parents=True)
        with contextlib.redirect_stdout(io.StringIO()):
            review.start(self.manifest, 12345, self.head, str(out), 'ledger-12345')
        out.write_text('**BLOCK**\n')  # the reviewer wrote BLOCK and crashed before `record`
        self.manifest.review.result_globs = [str(self.root/'handoff/**/rresult*.out')]
        self.assertEqual([f['verdict'] for f in review.unledgered(self.manifest)], ['BLOCK'])

    def test_each_end_closes_only_its_own_start(self):
        from coordkit import review
        from coordkit.process import parse_ts
        out = self.root/'r.out'
        with contextlib.redirect_stdout(io.StringIO()):
            first = review.start(self.manifest, 12345, self.head, str(out), 'ledger-12345', round_=1)
            review.start(self.manifest, 12345, self.head, str(out), 'ledger-12345', round_=1)
            out.write_text('VERDICT: SHIP\n')
            review.record(self.manifest, 12345, self.head, str(out), 'ledger-12345', run=first, rc=0, post=False)
        rows = review.rows_of(self.manifest)
        late = parse_ts(rows[0]['ts']).timestamp() + 2*3600
        self.assertEqual(len(review.stale(self.manifest, at=late)), 1)

    def test_reused_result_file_is_unrecorded_again_and_run_is_required(self):
        from coordkit import review
        from coordkit.manifest import ManifestError
        out = self.root/'handoff/x/rresult.out'; out.parent.mkdir(parents=True)
        self.manifest.review.result_globs = [str(self.root/'handoff/**/rresult*.out')]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                patch.object(review, 'live_head', return_value=self.head):
            run = review.start(self.manifest, 12345, self.head, str(out), 'ledger-12345')
            out.write_text('VERDICT: SHIP\n')
            review.record(self.manifest, 12345, self.head, str(out), 'ledger-12345', run=run, rc=0, post=False)
            self.assertEqual(review.unledgered(self.manifest), [])
            again = review.start(self.manifest, 12345, self.head, str(out), 'ledger-12345')
            out.write_text('**BLOCK**\n')  # same path, new content, never recorded
            self.assertEqual([f['verdict'] for f in review.unledgered(self.manifest)], ['BLOCK'])
            with self.assertRaises(ManifestError):  # a run-id start needs --run
                review.record(self.manifest, 12345, self.head, str(out), 'ledger-12345', rc=0)

    def test_verdict_and_hash_come_from_one_read(self):
        from coordkit import review
        out = self.root/'race.out'
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                patch.object(review, 'live_head', return_value=self.head):
            run = review.start(self.manifest, 12345, self.head, str(out), 'ledger-12345')
            out.write_text('VERDICT: SHIP\n')
            real_open = open
            def racing_open(path, mode='r', *a, **k):
                handle = real_open(path, mode, *a, **k)
                if str(path) == str(out) and 'b' in mode:
                    out.write_text('**BLOCK**\n')  # a concurrent writer after our single read starts
                return handle
            with patch('builtins.open', racing_open):
                review.record(self.manifest, 12345, self.head, str(out), 'ledger-12345', run=run, rc=0, post=False)
        end = review.rows_of(self.manifest)[-1]
        self.assertEqual(end['sha256'], review.fingerprint(b'VERDICT: SHIP\n') if end['verdict'] == 'SHIP'
                         else review.fingerprint(b'**BLOCK**\n'))
        self.manifest.review.result_globs = [str(out)]
        if end['verdict'] == 'SHIP':
            self.assertEqual([f['verdict'] for f in review.unledgered(self.manifest)], ['BLOCK'])
