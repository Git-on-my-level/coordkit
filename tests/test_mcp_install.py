import contextlib
import io
from unittest.mock import Mock, patch
from coordkit import mcp_install
from coordkit.manifest import ManifestError
from coordkit.state import read_json
from tests.support import FakeCase


class McpInstallTests(FakeCase):
    def test_install_private_secret_idempotent_and_uninstall_keeps_token(self):
        secret=self.root/'secrets/token'
        service=Mock()
        service.path.return_value=self.root/'unit'
        def install(spec,previous=False):
            self.assertEqual(read_json(self.manifest.paths.root/'mcp-service.json'),spec)
            service.path.return_value.touch()
        service.install.side_effect=install
        with patch.object(mcp_install,'manager',return_value=service),contextlib.redirect_stdout(io.StringIO()):
            for _ in range(2):
                mcp_install.main(['install','--secret-file',str(secret)],self.manifest)
            self.assertEqual(service.install.call_count,1)
            self.assertEqual(secret.stat().st_mode & 0o777,0o600)
            self.assertGreater(len(secret.read_text().strip()),32)
            mcp_install.main(['uninstall'],self.manifest)
        self.assertTrue(secret.exists())
        self.assertFalse((self.manifest.paths.root/'mcp-service.json').exists())
        service.uninstall.assert_called_once_with(self.manifest.launchd.label_prefix+'.mcp')

    def test_shadow_refuses_before_secret_or_services(self):
        self.manifest.mode='shadow'
        secret=self.root/'token'
        with self.assertRaises(ManifestError):
            mcp_install.main(['install','--secret-file',str(secret)],self.manifest)
        self.assertFalse(secret.exists())
