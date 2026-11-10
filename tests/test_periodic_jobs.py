"""Periodic service failures and coordkit runtime skew are doctor findings."""
import plistlib
from pathlib import Path
import sys
from unittest.mock import patch

from coordkit import __version__, lifecycle, services
from coordkit.manifest import ManifestError
from tests.support import FakeCase


class PeriodicJobTests(FakeCase):
    def setUp(self):
        super().setUp()
        self.manifest.launchd.path = [str(Path.home()/'.local/bin'), '/usr/bin', '/bin']

    def source(self, name, version):
        root = self.root/name
        (root/'coordkit').mkdir(parents=True)
        (root/'coordkit/__init__.py').write_text(f'__version__ = "{version}"\n')
        return root

    def detail(self, path, job, status):
        args = '\n'.join('"' + arg + '"' for arg in job['ProgramArguments'])
        path_value = job['EnvironmentVariables']['PATH']
        return (f'path = {path}\nenvironment = {{\nPATH => {path_value}\n}}\n'
                f'arguments = {{\n{args}\n}}\nrun interval = {job["StartInterval"]} seconds\n'
                f'last exit code = {status}\n')

    def observed_job(self, label='com.example.coordkit-wakescan'):
        old = self.source('pinned-0.3.0', '0.3.0')
        job = {'Label': label,
               'ProgramArguments': [sys.executable, '-m', 'coordkit.cli', '--manifest', str(self.file), 'wake-scan'],
               'WorkingDirectory': str(self.root),
               'EnvironmentVariables': {'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(old)},
               'StartInterval': 30}
        path = self.root/'observed.plist'
        path.write_bytes(plistlib.dumps(job))
        return job, path

    def test_managed_periodic_failure_and_runtime_version_are_drift(self):
        job = lifecycle.templates(self.manifest)[0]
        old = self.source('old-managed', '0.3.0')
        job['EnvironmentVariables']['PYTHONPATH'] = str(old)
        path = self.root/'managed.plist'
        path.write_bytes(plistlib.dumps(job))
        with patch.object(services, 'checked', side_effect=[f'1 2 {job["Label"]}\n', self.detail(path, job, 2)]):
            drift = lifecycle.installation_drift(self.manifest)
        self.assertIn(f'periodic job failed {job["Label"]}: last exit code 2', drift)
        self.assertIn(f'coordkit version skew {job["Label"]}: job=0.3.0 doctor={__version__}', drift)

    def test_observed_external_wake_scan_failure_and_skew_are_red(self):
        job, path = self.observed_job()
        self.manifest.launchd.observed_labels = [job['Label']]
        with patch.object(services.Launchd, 'detail', return_value=self.detail(path, job, 2)), \
                patch.object(lifecycle.hooks, 'drift', return_value=[]), \
                patch.object(lifecycle.hooks, 'question_drift', return_value=[]):
            drift = lifecycle.installation_drift(self.manifest)
        self.assertIn(f'periodic job failed {job["Label"]}: last exit code 2', drift)
        self.assertIn(f'coordkit version skew {job["Label"]}: job=0.3.0 doctor={__version__}', drift)
        current = self.source('current', __version__)
        job['EnvironmentVariables']['PYTHONPATH'] = str(current)
        path.write_bytes(plistlib.dumps(job))
        with patch.object(services.Launchd, 'detail', return_value=self.detail(path, job, 0)):
            self.assertEqual(lifecycle._observed_job_drift(self.manifest), [])

    def test_observation_fails_closed_without_manifest_or_version_evidence(self):
        job, path = self.observed_job()
        self.manifest.launchd.observed_labels = [job['Label']]
        job['EnvironmentVariables'].pop('PYTHONPATH')
        path.write_bytes(plistlib.dumps(job))
        with patch.object(services.Launchd, 'detail', return_value=self.detail(path, job, 0)):
            drift = lifecycle._observed_job_drift(self.manifest)
        self.assertIn(f'coordkit version unavailable {job["Label"]}: readable PYTHONPATH required', drift)
        job['ProgramArguments'][job['ProgramArguments'].index('--manifest') + 1] = str(self.root/'other.toml')
        path.write_bytes(plistlib.dumps(job))
        with patch.object(services.Launchd, 'detail', return_value=self.detail(path, job, 0)):
            drift = lifecycle._observed_job_drift(self.manifest)
        self.assertTrue(any(item.startswith('observed job manifest drift') for item in drift))

    def test_observed_labels_must_be_unique_and_outside_managed_namespace(self):
        self.file.write_text(self.file.read_text() +
                             '\n[launchd]\nobserved_labels = ["com.coordkit.sample.reconcile"]\n')
        with self.assertRaises(ManifestError):
            from coordkit.manifest import load
            load(self.file)
