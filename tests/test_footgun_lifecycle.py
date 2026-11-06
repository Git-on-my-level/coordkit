import dataclasses
import contextlib
import io
import os
import plistlib
import time
from unittest.mock import patch
from coordkit import lifecycle
from coordkit.state import write_json
from tests.support import FakeCase


class LifecycleFootgunTests(FakeCase):
    def setUp(self):
        super().setUp()
        self.manifest.launchd.path=[str(__import__('pathlib').Path.home()/'.local/bin'),'/usr/bin','/bin']

    def test_p2_8_down_finds_unrecorded_jobs_with_missing_state(self):
        output='PID Status Label\n123 0 com.coordkit.sample.orphan\n123 0 com.coordkit.sample-other.keep\n'
        with patch.object(lifecycle,'checked',return_value=output) as checked, patch.object(lifecycle,'run') as run:
            run.return_value.returncode=0
            with contextlib.redirect_stdout(io.StringIO()): lifecycle.down(self.manifest)
        bootouts=[call.args[0] for call in checked.call_args_list if 'bootout' in call.args[0]]
        self.assertEqual(bootouts, [lifecycle.launchctl('bootout',f'gui/{os.getuid()}/com.coordkit.sample.orphan')])

    def test_p2_8_doctor_diffs_actual_loaded_plist_and_argv(self):
        job=lifecycle.templates(self.manifest)[0]
        path=self.root/'out-of-band.plist'
        path.write_bytes(plistlib.dumps(dict(job, StartInterval=999)))
        raw=f'path = {path}\nenvironment = {{\nPATH => '+job['EnvironmentVariables']['PATH']+'\n}\narguments = {\nwrong\n}\nrun interval = 999 seconds\n'
        with patch.object(lifecycle,'checked',side_effect=[f'1 0 {job["Label"]}\n',raw]):
            drift=lifecycle.installation_drift(self.manifest)
        self.assertTrue(any('plist drift' in d for d in drift))
        self.assertTrue(any('arguments drift' in d for d in drift))
        self.assertTrue(any('interval drift' in d for d in drift))

    def test_p3_10_doctor_red_for_old_reconciler(self):
        write_json(self.manifest.paths.root/'reconcile/state.json',{'last_run':time.time()-601})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(lifecycle.doctor(self.manifest),1)
        self.assertIn('DOCTOR reconciler_age red',out.getvalue())

    def test_shadow_doctor_ignores_reconcile_stamp_left_by_a_rolled_back_apply(self):
        write_json(self.manifest.paths.root/'reconcile/state.json',{'last_run':time.time()-601})
        shadow=dataclasses.replace(self.manifest, mode='shadow')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            lifecycle.doctor(shadow)
        self.assertIn('DOCTOR reconciler_age green shadow: reconciler not scheduled',out.getvalue())
