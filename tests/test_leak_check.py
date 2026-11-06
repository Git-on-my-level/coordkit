"""The publication grep gate fails closed, with exact example-only exceptions."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class LeakCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'scripts').mkdir()
        self.allow = self.root/'scripts/leak-allowlist.txt'
        self.allow.write_text('')
        shutil.copyfile(Path(__file__).resolve().parents[1]/'scripts/leak-check.sh', self.root/'scripts/leak-check.sh')

    def run_gate(self):
        return subprocess.run(['bash', str(self.root/'scripts/leak-check.sh')], capture_output=True, text=True, timeout=10)

    def test_archive_without_git_metadata_passes(self):
        (self.root/'README.md').write_text('A neutral local message bus.\n')
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('0 unapproved hits', result.stdout)

    def test_deny_patterns_fail_in_public_content(self):
        patterns = ['Da'+'vid', r'\b'+'Da'+'vid', 'Lu'+'na', '/Us'+'ers/private', '/Vol'+'umes/private',
                    'da'+'zheng', 'da'+'vidzhang', 'm'+'5-mbp', 'Based'+'Hardware',
                    'for_'+'da'+'vid', 'DA'+'VID_OR_SYSTEM', 'needsDa'+'vidApproval',
                    'my'+'space', 'br'+'amp2', 'roll'+'eff', 'cl2'+'d-123a',
                    'm'+'5-air', 'm'+'4-air', 'm'+'1-studio',
                    'lead'+'2', 'cl'+'2-worker', 'speed'+'lead', '100.'+'90.1.2', 'fd7a:'+'115c:a1e0:1234']
        target = self.root/'README.md'
        for pattern in patterns:
            with self.subTest(pattern=pattern):
                target.write_text(pattern+'\n')
                self.assertNotEqual(self.run_gate().returncode, 0)

    def test_only_exact_allowlisted_example_lines_pass(self):
        path = 'examples/omi/config.toml'
        target = self.root/path; target.parent.mkdir(parents=True)
        line = 'family = "'+'lu'+'na"'
        target.write_text(line+'\n')
        self.allow.write_text(path+':'+hashlib.sha256(line.encode()).hexdigest()+'\n')
        self.assertEqual(self.run_gate().returncode, 0)
        target.write_text(line+' # changed\n')
        self.assertNotEqual(self.run_gate().returncode, 0)

    def test_allowlist_cannot_exempt_another_directory_or_stale_line(self):
        self.allow.write_text('README.md:'+64*'a'+'\n')
        self.assertIn('invalid allow-list path', self.run_gate().stderr)
        self.allow.write_text('examples/omi/config.toml:'+64*'a'+'\n')
        self.assertIn('stale allow-list', self.run_gate().stderr)
