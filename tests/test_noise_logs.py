import contextlib
import io
import os
from unittest.mock import patch
from coordkit import bus, merge, reconcile, review
from coordkit.state import read_json
from tests.support import FakeCase


class NoiseLogTests(FakeCase):
    def test_review_scan_prints_one_summary_and_relays_each_new_file_once(self):
        self.manifest.review.ledger = 'review-ledger.jsonl'
        self.manifest.review.result_globs = [str(self.root/'*.out')]
        self.manifest.addressed.relay_file = 'pending-alerts.ndjson'
        for name, stamp in [('new', 200), ('old', 100), ('middle', 150)]:
            p = self.root/(name + '.out')
            p.write_text('VERDICT: BLOCK\n')
            os.utime(p, (stamp, stamp))
        with patch('time.time', return_value=300):
            for _ in range(2):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(review.main(['scan', '--relay'], self.manifest), 5)
                self.assertEqual(out.getvalue().splitlines(),
                    [f'REVIEW_UNLEDGERED count=3 oldest={self.root}/old.out'])
        self.assertEqual(len((self.root/'pending-alerts.ndjson').read_text().splitlines()), 3)

    def test_reconcile_prints_only_new_ignored_row_pr_pairs_across_process_state(self):
        bus.configure(self.manifest)
        bus.append([{'id': 'ignored', 'frm': 'worker', 'to': 'lead', 'kind': 'fyi',
                     'ts': '1970-01-01T00:00:01Z', 'text': 'hold #123 and #124'}], project=False)
        for iteration, expected in enumerate((2, 0)):
            merge._IGNORING.clear()  # a fresh invocation cannot depend on the in-process cache
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                reconcile.reconcile(self.manifest, at=10000+iteration)
            self.assertEqual(out.getvalue().count('MERGE_HOLD_TEXT_IGNORED'), expected)
        state = read_json(self.manifest.paths.root/'reconcile/state.json')
        self.assertIn('hold-text:ignored:123', state['fired'])
        bus.append([{'id': 'new', 'frm': 'worker', 'to': 'lead', 'kind': 'fyi',
                     'ts': '1970-01-01T00:00:01Z', 'text': 'hold #123'}], project=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            reconcile.reconcile(self.manifest, at=10002)
        self.assertEqual(out.getvalue().count('MERGE_HOLD_TEXT_IGNORED'), 1)
        self.assertIn('MERGE_HOLD_TEXT_IGNORED new', out.getvalue())
