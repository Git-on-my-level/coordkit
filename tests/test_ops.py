import json
import unittest
from pathlib import Path
from coordkit import ops, mcp_server


class OperationTests(unittest.TestCase):
    def test_mcp_wire_schemas_only_add_optional_handoff_fields(self):
        expected = json.loads((Path(__file__).parent/'fixtures/mcp-tools-v01.json').read_text())
        post = next(tool for tool in expected if tool['name'] == 'post')
        post['inputSchema']['properties'].update(
            ref={'type': 'string', 'maxLength': 4096},
            evidence={'type': 'string', 'maxLength': 4096},
            degrade_handoff={'type': 'string', 'minLength': 1, 'maxLength': 500})
        self.assertEqual(mcp_server.TOOLS, expected)
        self.assertEqual(mcp_server.WRITES, {o.name[4:] for o in ops.MCP_OPS if o.mode == 'write'})

    def test_docs_match_registry(self):
        self.assertEqual((Path(__file__).parents[1]/'docs/OPS.md').read_text(), ops.markdown())

    def test_registry_unique_and_parser_uses_args(self):
        names = [o.name for o in ops.OPERATIONS]
        self.assertEqual(len(names), len(set(names)))
        parsed = ops.bus_parser().parse_args(['post','--from','sender','--to','all','body'])
        self.assertEqual((parsed.frm, parsed.kind, parsed.text), ('sender','msg','body'))
