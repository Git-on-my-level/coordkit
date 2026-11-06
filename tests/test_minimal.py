import contextlib
import io
import subprocess
from unittest.mock import patch
from coordkit import lifecycle, services
from tests.support import FakeCase


class MinimalTests(FakeCase):
    def test_minimal_init_and_doctor_needs_no_external_adapters(self):
        target=self.root/'minimal.toml'
        with contextlib.redirect_stdout(io.StringIO()):
            lifecycle.initialize('minimal',target,'anx',None,'owner/repo',profile='minimal')
        from coordkit.manifest import load
        cfg=load(target)
        cfg.services.manager='systemd'
        self.assertFalse(cfg.anx.workspace)
        with patch.object(services.process,'run',return_value=subprocess.CompletedProcess([],0,'','')), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(lifecycle.doctor(cfg),0,output.getvalue())
        self.assertFalse(any(c['tool']=='anx' for c in self.calls()))
