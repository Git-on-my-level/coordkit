import contextlib
import io
import json
from unittest.mock import Mock, patch
from coordkit import mcp_install, mcp_server
from coordkit.manifest import ManifestError
from coordkit.state import read_json
from tests.support import FakeCase


class McpInstallTests(FakeCase):
    def setUp(self):
        super().setUp()
        self.clients = self.root/'clients.json'
        patcher = patch.object(mcp_server, 'CLIENTS_FILE', self.clients)
        patcher.start(); self.addCleanup(patcher.stop)

    def test_install_private_secret_idempotent_and_uninstall_keeps_token(self):
        secret=self.root/'secrets/token'
        secret.parent.mkdir()
        secret.write_text('existing-legacy-secret-' * 3); secret.chmod(0o600)
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

    def test_named_install_and_reinstall_never_restore_retired_legacy_access(self):
        # Service lifecycle must not mint an all-powerful fallback after migration.
        self.clients.write_text(json.dumps({'reader': {'secret': 'n' * 43}})); self.clients.chmod(0o600)
        secret = self.root/'connector.secret'
        service = Mock()
        service.path.return_value = self.root/'unit'
        service.install.side_effect = lambda spec, previous=False: service.path.return_value.touch()
        argv = ['install', '--clients-file', str(self.clients), '--secret-file', str(secret)]
        with patch.object(mcp_install, 'manager', return_value=service), \
                patch.object(mcp_install, 'register'), contextlib.redirect_stdout(io.StringIO()):
            mcp_install.main(argv, self.manifest)
            self.assertFalse(secret.exists())
            args = read_json(self.manifest.paths.root/'mcp-service.json')['ProgramArguments']
            self.assertEqual(args[args.index('--clients-file') + 1], str(self.clients))
            # Existing legacy credentials are an explicit opt-in; retain migration compatibility.
            secret.write_text('s' * 43); secret.chmod(0o600)
            mcp_install.main(argv, self.manifest)
            self.assertEqual(mcp_server.authenticate(self.manifest, {'Authorization': 'Bearer ' + 's' * 43}, secret, self.clients).id, 'legacy')
            secret.unlink()
            for _ in range(2):
                mcp_install.main(['uninstall'], self.manifest)
                mcp_install.main(argv, self.manifest)
                self.assertFalse(secret.exists())
                self.assertIsNone(mcp_server.authenticate(self.manifest, {'Authorization': 'Bearer ' + 's' * 43}, secret, self.clients))
                client = mcp_server.authenticate(self.manifest, {'Authorization': 'Bearer ' + 'n' * 43}, secret, self.clients)
                self.assertEqual(client.id, 'reader')
                self.assertFalse(client.write); self.assertFalse(client.answer)

    def test_fresh_install_requires_credentials_before_writing_receipt_or_service(self):
        secret = self.root/'missing-directory'/'connector.secret'
        service = Mock()
        with patch.object(mcp_install, 'manager', return_value=service), \
                self.assertRaisesRegex(ManifestError, 'mcp_credentials_required'):
            mcp_install.main(['install', '--secret-file', str(secret)], self.manifest)
        self.assertFalse(secret.parent.exists())
        self.assertFalse((self.manifest.paths.root/'mcp-service.json').exists())
        service.install.assert_not_called()

    def test_invalid_named_credentials_cannot_trigger_legacy_creation(self):
        self.clients.write_text('{'); self.clients.chmod(0o600)
        secret = self.root/'connector.secret'
        service = Mock()
        with patch.object(mcp_install, 'manager', return_value=service), self.assertRaises(ManifestError):
            mcp_install.main(['install', '--secret-file', str(secret)], self.manifest)
        self.assertFalse(secret.exists())
        self.assertFalse((self.manifest.paths.root/'mcp-service.json').exists())
        service.install.assert_not_called()
