import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import zipfile
from unittest.mock import Mock, patch
from coordkit import selfupdate as update
from coordkit.manifest import ManifestError
from coordkit.state import read_json, write_json
from tests.support import FakeCase


def wheel(version, unsafe=False):
    data=io.BytesIO()
    with zipfile.ZipFile(data,'w') as archive:
        archive.writestr('coordkit/__init__.py',f'__version__ = "{version}"\n')
        archive.writestr('coordkit/cli.py','def main(argv):\n    print("coordkit")\n')
        archive.writestr('coordkit-'+version+'.dist-info/METADATA',f'Name: coordkit\nVersion: {version}\n')
        archive.writestr('coordkit-'+version+'.dist-info/WHEEL','Root-Is-Purelib: true\nTag: py3-none-any\n')
        if unsafe: archive.writestr('../escape','bad')
    return data.getvalue()


class UpdateTests(FakeCase):
    def setUp(self):
        super().setUp()
        self.env=patch.dict(os.environ,COORDKIT_INSTALL_HOME=str(self.root/'install'),COORDKIT_BIN_HOME=str(self.root/'bin'))
        self.env.start(); self.addCleanup(self.env.stop)

    def release(self,version):
        name='coordkit-'+version+'-py3-none-any.whl'
        body=wheel(version)
        release=dict(tag_name='v'+version,assets=[dict(name=name,browser_download_url='https://test/wheel'),dict(name='SHA256SUMS',browser_download_url='https://test/sums')])
        def fetch(url,*args):
            if url.endswith('/wheel'): return body
            if url.endswith('/sums'): return (hashlib.sha256(body).hexdigest()+'  '+name+'\n').encode()
            return json.dumps(release).encode()
        return release, fetch

    def apply(self,version,**kwargs):
        release, fetch=self.release(version)
        with patch.object(update,'fetch',side_effect=fetch),contextlib.redirect_stdout(io.StringIO()):
            update.apply(release,**kwargs)

    def test_adopt_upgrade_rollback_and_prune(self):
        self.apply('0.2.0',adopt=True)
        first=(update.home()/'current').resolve()
        self.assertTrue((self.root/'bin/coordkit').is_file())
        self.apply('0.2.1')
        self.assertEqual((update.home()/'current').resolve().name,'v0.2.1')
        with patch.object(update,'reconcile'),contextlib.redirect_stdout(io.StringIO()): update.rollback()
        self.assertEqual((update.home()/'current').resolve(),first)
        self.apply('0.2.2')
        with contextlib.redirect_stdout(io.StringIO()): update.prune(1)
        self.assertTrue(first.exists())  # last rollback anchor survives
        self.assertTrue((update.home()/'current').exists())

    def test_checksum_smoke_and_reconcile_failure_preserve_current(self):
        self.apply('0.2.0',adopt=True)
        old=(update.home()/'current').resolve()
        release,fetch=self.release('0.2.1')
        with patch.object(update,'fetch',side_effect=lambda url,*a: b'bad' if url.endswith('/sums') else fetch(url,*a)),self.assertRaises(ManifestError):
            update.apply(release)
        with patch.object(update,'fetch',side_effect=fetch),patch.object(update,'smoke',side_effect=ManifestError('smoke')),self.assertRaises(ManifestError):
            update.apply(release)
        with patch.object(update,'fetch',side_effect=fetch),patch.object(update,'reconcile',side_effect=ManifestError('hook conflict')),self.assertRaises(ManifestError):
            update.apply(release)
        self.assertEqual((update.home()/'current').resolve(),old)

    def test_archive_traversal_and_metadata_refused(self):
        with self.assertRaisesRegex(ManifestError,'unsafe_wheel'):
            update.unpack(wheel('0.2.0',unsafe=True),self.root/'stage','v0.2.0')
        with self.assertRaisesRegex(ManifestError,'version_mismatch'):
            update.unpack(wheel('0.2.0'),self.root/'stage','v0.3.0')
        self.assertFalse((self.root/'escape').exists())

    def test_notify_only_unmanaged_and_owned_shim_conflict(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.apply('0.2.0')
        self.assertFalse((update.home()/'current').exists())
        target=self.root/'bin/coordkit'; target.parent.mkdir(); target.write_text('user command')
        with self.assertRaisesRegex(ManifestError,'shim_conflict'): self.apply('0.2.0',adopt=True)
        self.assertEqual(target.read_text(),'user command')
        self.assertFalse((update.home()/'current').exists())

    def test_check_state_fetch_timeout_and_no_asset_token(self):
        release,fetch=self.release('0.2.0')
        with patch.object(update,'fetch',side_effect=fetch): self.assertEqual(update.check()['tag_name'],'v0.2.0')
        self.assertEqual(read_json(update.state_dir()/'update-policy.json')['mode'],'notify')
        response=Mock(); response.__enter__=Mock(return_value=response); response.__exit__=Mock(return_value=False); response.read.return_value=b'ok'
        with patch.dict(os.environ,GITHUB_TOKEN='not-a-real-secret'),patch.object(update.urllib.request,'urlopen',return_value=response) as opener:
            self.assertEqual(update.fetch('https://test/asset'),b'ok')
            self.assertNotIn('Authorization',opener.call_args.args[0].headers)
            self.assertEqual(opener.call_args.kwargs['timeout'],10)
        with patch.object(update,'latest',side_effect=OSError('offline')),self.assertRaises(OSError): update.check()
        self.assertEqual(read_json(update.state_dir()/'update-state.json')['last_error'],'OSError')

    def test_manifest_registry_preserves_extra_jobs(self):
        update.register(self.manifest,['extra.mcp'])
        update.register(self.manifest)
        self.assertEqual(read_json(update.state_dir()/'manifests.json')[str(self.file)]['extra_jobs'],['extra.mcp'])

    def test_first_adoption_reconcile_failure_leaves_no_shim(self):
        with patch.object(update, 'reconcile', side_effect=ManifestError('reconcile failed')), self.assertRaises(ManifestError):
            self.apply('0.2.0', adopt=True)
        self.assertFalse((update.home()/'current').is_symlink())
        self.assertFalse((self.root/'bin/coordkit').exists())

    def test_first_adoption_post_swap_smoke_failure_leaves_no_shim(self):
        original = update.smoke
        def smoke(path, version):
            if path.name == 'current':
                raise ManifestError('activated smoke failed')
            original(path, version)
        with patch.object(update, 'smoke', side_effect=smoke), self.assertRaises(ManifestError):
            self.apply('0.2.0', adopt=True)
        self.assertFalse((update.home()/'current').is_symlink())
        self.assertFalse((self.root/'bin/coordkit').exists())

    def test_first_adoption_installs_shim_after_reconciliation(self):
        reconciled = False
        actual_shim = update.shim
        def reconcile(*args):
            nonlocal reconciled
            self.assertTrue((update.home()/'current').is_dir())
            self.assertFalse((self.root/'bin/coordkit').exists())
            reconciled = True
        def shim():
            self.assertTrue(reconciled)
            actual_shim()
        with patch.object(update, 'reconcile', side_effect=reconcile), patch.object(update, 'shim', side_effect=shim):
            self.apply('0.2.0', adopt=True)
        self.assertTrue((self.root/'bin/coordkit').exists())

    def test_health_maps_dangling_and_looping_current_to_manifest_error(self):
        root = update.home(); root.mkdir(parents=True)
        current = root/'current'
        for target in (root/'missing-release', current):
            with self.subTest(target=target):
                current.symlink_to(target)
                try:
                    with self.assertRaisesRegex(ManifestError, 'selfupdate_invalid_current'):
                        update.health()
                finally:
                    current.unlink()
