import plistlib
import re
import subprocess
import unittest
from pathlib import Path
from coordkit.manifest import load


class ExampleHygieneTests(unittest.TestCase):
    def test_reference_manifest_uses_neutral_identities_and_train_pattern(self):
        root = Path(__file__).parents[1]
        manifest = load(root/'examples/omi/coordkit.toml')
        self.assertEqual(manifest.questions.human_actor, 'operator')
        self.assertEqual(manifest.mcp.identity, 'example-mcp')
        self.assertEqual(manifest.review.family, 'ledger')
        self.assertEqual(manifest.mcp.train_pattern, r'\btrain-\d+\b')
        self.assertTrue(re.search(manifest.mcp.train_pattern, 'release train-123 ready'))
        self.assertFalse(re.search(manifest.mcp.train_pattern, 'release other-123 ready'))
        self.assertEqual(manifest.addressed.agents['worker-a']['runtime'], 'claude-code')
        self.assertFalse([line for line in (root/'scripts/leak-allowlist.txt').read_text().splitlines()
                          if line.strip() and not line.startswith('#')])

    def test_examples_have_no_operator_home_paths_and_scripts_parse(self):
        root = Path(__file__).parents[1]/'examples/omi'
        for path in root.rglob('*'):
            if not path.is_file() or path.suffix == '.pyc':
                continue
            with self.subTest(path=path.relative_to(root)):
                text = path.read_text()
                self.assertNotIn('$HOME', text)
                self.assertNotIn('~/', text)
                self.assertNotIn('Path.home()', text)
                if path.suffix == '.plist':
                    plistlib.loads(path.read_bytes())
                elif text.startswith('#!/bin/'):
                    result = subprocess.run(['bash', '-n', str(path)], capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_runtime_example_uses_default_budget_without_deployment_detail(self):
        from coordkit.manifest import Lane
        manifest = load(Path(__file__).parents[1]/'examples/omi/coordkit.toml')
        self.assertEqual(manifest.lane.max_run_hours, Lane().max_run_hours)
        text = manifest.file.read_text().lower()
        self.assertNotRegex(text, r'\bomi\b')
        self.assertNotIn('long missions', text)

    def test_reference_deployment_keeps_directory_and_uses_generic_guard_examples(self):
        root = Path(__file__).parents[1]/'examples/omi'
        manifest = load(root/'coordkit.toml')
        self.assertEqual(manifest.questions.guarded_patterns,
                         [r'\bdeploy\b', r'\bdelete\b', r'\bcredentials?\b'])
        for text in ('deploy the sample', 'delete the sample', 'update a credential'):
            self.assertTrue(any(re.search(pattern, text) for pattern in manifest.questions.guarded_patterns))
        self.assertEqual((root/'CUTOVER.md').read_text().splitlines()[0], '# Reference deployment cutover')
        self.assertIn('Reference deployment', (root/'bin/apply-cutover.sh').read_text().splitlines()[2])
