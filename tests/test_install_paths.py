"""Rendered hooks and jobs must follow a stable install, or use their wheel interpreter."""
import contextlib
import io
import os
from pathlib import Path
import shlex
import subprocess
import sys
from unittest.mock import patch

from coordkit import cli, deliver, hooks, lifecycle, paths
from tests.support import FakeCase


class InstallPathTests(FakeCase):
    def setUp(self):
        super().setUp()
        self.manifest.launchd.path = [str(Path.home()/'.local/bin'), '/usr/bin', '/bin']
        self.env = patch.dict(os.environ)
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop('COORDKIT_HOME', None)

    def rendered(self):
        return [hooks.command(self.manifest, 'pending'),
                hooks.question_command(self.manifest, 'Stop', self.root/'config with spaces'),
                deliver.command(self.manifest)]

    def assert_layout(self, root):
        for command in self.rendered():
            argv = shlex.split(command)
            self.assertIn(self.manifest.launchd.python, argv)
            self.assertIn(str(self.file), argv)
            assignments = [a for a in argv if a.startswith('PYTHONPATH=')]
            self.assertEqual(assignments, [] if root is None else ['PYTHONPATH='+str(root)])
        for job in lifecycle.templates(self.manifest):
            self.assertEqual(job['EnvironmentVariables'].get('PYTHONPATH'), None if root is None else str(root))

    def test_source_checkout_preserves_import_path(self):
        root = Path(paths.__file__).resolve().parent.parent
        self.assertEqual(paths.install_root(), root)
        self.assert_layout(root)

    def test_wheel_and_distribution_installs_do_not_bake_pythonpath(self):
        for layout in ('site-packages', 'dist-packages'):
            with self.subTest(layout=layout), patch.object(paths, '__file__', str(self.root/layout/'coordkit/paths.py')):
                self.assertIsNone(paths.install_root())
                self.assert_layout(None)

    def test_explicit_home_handles_spaces_and_checkout_src(self):
        home = self.root/'install with spaces'
        (home/'src/coordkit').mkdir(parents=True)
        with patch.dict(os.environ, COORDKIT_HOME=str(home)):
            self.assert_layout(home/'src')
        with patch.dict(os.environ, COORDKIT_HOME=str(home/'src')):
            self.assert_layout(home/'src')

    def test_current_symlink_survives_release_switch_for_all_builders(self):
        releases = self.root/'releases'
        for version in ('v1', 'v2'):
            (releases/version/'src/coordkit').mkdir(parents=True)
        current = self.root/'current'
        current.symlink_to(releases/'v1', target_is_directory=True)
        with patch.object(paths, '__file__', str(current/'src/coordkit/paths.py')):
            self.assert_layout(current/'src')
            before = self.rendered()
            replacement = self.root/'next'
            replacement.symlink_to(releases/'v2', target_is_directory=True)
            replacement.replace(current)
            self.assertEqual(before, self.rendered())
            self.assert_layout(current/'src')
        with patch.object(paths, '__file__', str(releases/'v2/src/coordkit/paths.py')):
            self.assert_layout(current/'src')

    def test_flat_release_import_root_is_stable(self):
        with patch.object(paths, '__file__', str(self.root/'current/coordkit/paths.py')):
            self.assert_layout(self.root/'current')

    def test_version_needs_no_manifest_or_external_tools(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as result:
            cli.main(['--version'])
        self.assertEqual(result.exception.code, 0)
        self.assertEqual(output.getvalue(), 'coordkit 0.1.0\n')
        env = dict(os.environ, PYTHONPATH=str(Path(paths.__file__).resolve().parent.parent))
        result = subprocess.run([sys.executable, '-m', 'coordkit.cli', '--version'], cwd=self.root,
                                env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, 'coordkit 0.1.0'))
