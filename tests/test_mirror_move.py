import io
import unittest
from contextlib import redirect_stderr

from coordkit.mirror import move_to_review
from coordkit.process import ToolError


class StubClient:
    def __init__(self, failures):
        self.failures = list(failures)
        self.calls = 0

    def call(self, *args, write=False):
        self.calls += 1
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
        self.assertIn('MIRROR_MOVE_SKIPPED handoff r1', err.getvalue())

    def test_other_errors_still_raise(self):
        client = StubClient([ToolError('anx', 6, 'down', 'tool_failure')])
        with self.assertRaises(ToolError):
            move_to_review(client, 'card:x', 'r1', pause=0)
        self.assertEqual(client.calls, 1)


if __name__ == '__main__':
    unittest.main()
