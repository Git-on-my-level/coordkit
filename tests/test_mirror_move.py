import io
import unittest
from contextlib import redirect_stderr

from coordkit.mirror import board_write, move_to_review
from coordkit.process import ToolError


class StubClient:
    def __init__(self, failures):
        self.failures = list(failures)
        self.calls = 0

    def call(self, *args, write=False):
        self.calls += 1
        self.last = args
        if self.failures:
            raise self.failures.pop(0)
        return {}


def conflict():
    return ToolError('anx', 4, 'board has been updated; refresh and retry', 'conflict')


class MoveToReviewTests(unittest.TestCase):
    def test_conflict_then_success_retries(self):
        client = StubClient([conflict()])
        move_to_review(client, 'card:x', 'r1', pause=0)
        self.assertEqual(client.calls, 2)

    def test_persistent_conflict_skips_without_raising(self):
        client = StubClient([conflict()] * 3)
        err = io.StringIO()
        with redirect_stderr(err):
            move_to_review(client, 'card:x', 'r1', pause=0)
        self.assertEqual(client.calls, 3)
        self.assertIn('MIRROR_BOARD_SKIPPED move card:x r1', err.getvalue())

    def test_other_errors_still_raise(self):
        client = StubClient([ToolError('anx', 6, 'down', 'tool_failure')])
        with self.assertRaises(ToolError):
            move_to_review(client, 'card:x', 'r1', pause=0)
        self.assertEqual(client.calls, 1)


class BoardWriteTests(unittest.TestCase):
    def test_hold_create_conflict_is_skipped_not_raised(self):
        client = StubClient([conflict()] * 3)
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertFalse(board_write(client, 'h1', 'hold PR 142', 'cards', 'create', '--handle', 'hold-pr-142-w', pause=0))
        self.assertIn('MIRROR_BOARD_SKIPPED hold PR 142 h1', err.getvalue())


class UnholdProjectionTests(unittest.TestCase):
    def test_unhold_resolve_carries_evidence_body(self):
        from unittest.mock import patch
        from coordkit import mirror as mirror_module
        client = StubClient([])
        manifest = type('M', (), {})()
        rows = [{'id': 'h1', 'kind': 'hold', 'frm': 'worker', 'prs': ['142'], 'text': 'wait'},
                {'id': 'u1', 'kind': 'unhold', 'frm': 'worker', 'prs': ['142'], 'text': 'clear'}]
        with patch.object(mirror_module, 'AnxClient', return_value=client):
            mirror_module.mirror(manifest, rows[1], rows)
        self.assertEqual(client.last[:3], ('cards', 'resolve', 'card:hold-pr-142-worker'))
        self.assertIn('--body', client.last)
        self.assertEqual(client.last[client.last.index('--body') + 1], 'unhold u1 (worker): clear')


if __name__ == '__main__':
    unittest.main()
