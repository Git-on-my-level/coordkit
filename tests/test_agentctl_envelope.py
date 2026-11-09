"""agentctl envelope contract: coordkit reads schema_version 1 and fails closed on anything else.

The producer's envelope is documented in hostctl's docs/conventions/JSON-CONTRACT.md.
These tests use a test-created fake executable, never the user's agentctl."""
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

from coordkit.actl import AgentctlClient
from coordkit.process import ToolError, call_json


def fake_tool(directory, document):
    """Write an executable that prints `document` as its whole stdout and exits 0."""
    path = Path(directory) / "fake-agentctl"
    path.write_text(f"#!{sys.executable}\nimport sys\nsys.stdout.write({json.dumps(json.dumps(document))})\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


class EnvelopeSchemaTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def client_for(self, document):
        binary = fake_tool(self.dir, document)
        patcher = patch.dict(os.environ, {"AGENTCTL": binary})
        patcher.start()
        self.addCleanup(patcher.stop)
        return AgentctlClient(manifest=None)

    def test_schema_version_one_returns_the_result(self):
        client = self.client_for({"ok": True, "schema_version": 1, "result": {"executions": [], "has_more": False}, "warnings": [], "next_actions": []})
        self.assertEqual(client.recent(), [])

    def test_unsupported_schema_version_fails_closed(self):
        client = self.client_for({"ok": True, "schema_version": 2, "result": {"executions": [{"id": "partial"}], "has_more": False}})
        with self.assertRaises(ToolError) as caught:
            client.recent()
        self.assertEqual(caught.exception.category, "unsupported_schema")
        self.assertEqual(caught.exception.exit_code, 2)

    def test_missing_schema_version_fails_closed(self):
        client = self.client_for({"ok": True, "result": {"executions": [], "has_more": False}})
        with self.assertRaises(ToolError) as caught:
            client.recent()
        self.assertEqual(caught.exception.category, "unsupported_schema")

    def test_boolean_schema_version_is_not_version_one(self):
        # Python treats True == 1; the contract requires an integer.
        client = self.client_for({"ok": True, "schema_version": True, "result": {"executions": [], "has_more": False}})
        with self.assertRaises(ToolError) as caught:
            client.recent()
        self.assertEqual(caught.exception.category, "unsupported_schema")

    def test_string_schema_version_is_not_version_one(self):
        client = self.client_for({"ok": True, "schema_version": "1", "result": {"executions": [], "has_more": False}})
        with self.assertRaises(ToolError) as caught:
            client.recent()
        self.assertEqual(caught.exception.category, "unsupported_schema")

    def test_unsuccessful_envelope_keeps_its_error_category(self):
        error = {"code": "not_found", "message": "open journal", "retryable": False, "exit_code": 3, "details": {}, "next_actions": []}
        binary = fake_tool(self.dir, {"ok": False, "schema_version": 1, "error": error, "warnings": []})
        with patch.dict(os.environ, {"AGENTCTL": binary}):
            with self.assertRaises(ToolError) as caught:
                AgentctlClient(manifest=None).recent()
        self.assertEqual(caught.exception.category, "not_found")

    def test_recent_page_without_an_executions_list_is_a_protocol_error(self):
        client = self.client_for({"ok": True, "schema_version": 1, "result": {"has_more": False}})
        with self.assertRaises(ToolError) as caught:
            client.recent()
        self.assertEqual(caught.exception.category, "protocol_error")

    def test_call_json_without_a_schema_keeps_its_old_behaviour(self):
        # Callers outside agentctl (ANX) pass no schema and keep reading the result as before.
        binary = fake_tool(self.dir, {"ok": True, "result": {"card": {}}})
        self.assertEqual(call_json([binary]), {"card": {}})


if __name__ == "__main__":
    unittest.main()
