import contextlib
import io
import os
import subprocess
from unittest.mock import patch
from coordkit import lifecycle, services
from coordkit.manifest import ManifestError
from tests.support import FakeCase


class ServiceTests(FakeCase):
    def setUp(self):
        super().setUp()
        self.manifest.launchd.path=['/usr/bin',str(self.root)]
        # Service templates retain the interpreter/PATH validation contract.
        from pathlib import Path
        self.manifest.launchd.path.append(str(Path.home()/'.local/bin'))
        self.config = patch.dict(os.environ, XDG_CONFIG_HOME=str(self.root/'config'))
        self.config.start(); self.addCleanup(self.config.stop)
        self.manifest.services.manager='systemd'

    def test_platform_selection_and_rendering(self):
        self.manifest.services.manager=''
        with patch.object(services.sys,'platform','linux'):
            self.assertEqual(services.manager(self.manifest).name,'systemd')
        with patch.object(services.sys,'platform','darwin'):
            self.assertEqual(services.manager(self.manifest).name,'launchd')
        self.manifest.services.manager='systemd'
        spec=lifecycle.templates(self.manifest)[0]
        spec['ProgramArguments'].append('spaces " quote % $')
        rendered=services.manager(self.manifest).render(spec)
        self.assertIn('OnUnitActiveSec=',rendered[spec['Label']+'.timer'])
        self.assertIn('Persistent=true',rendered[spec['Label']+'.timer'])
        self.assertIn('%% $$',rendered[spec['Label']+'.service'])
        with self.assertRaises(ManifestError): services.quote('bad\nline')

    def test_install_drift_kick_uninstall_offline(self):
        service=services.manager(self.manifest)
        spec=lifecycle.templates(self.manifest)[0]
        calls=[]
        def fake(argv,**kw):
            calls.append(argv)
            if 'show' in argv:
                text='LoadState=loaded\nExecStart='+' '.join(spec['ProgramArguments'])+'\nEnvironment='+' '.join(k+'='+v for k,v in spec['EnvironmentVariables'].items())
            elif 'list-unit-files' in argv:
                text=spec['Label']+'.service static\n'+spec['Label']+'.timer enabled\nother.service enabled\n'
            else: text=''
            return subprocess.CompletedProcess(argv,0,text,'')
        with patch.object(services.process,'run',side_effect=fake):
            service.install(spec)
            self.assertEqual(service.drift(spec),[])
            self.assertEqual(service.installed_labels(self.manifest.launchd.label_prefix),[spec['Label']])
            service.kick(spec['Label'])
            service.path(spec['Label']).write_text('edited')
            self.assertTrue(service.drift(spec))
            service.uninstall(spec['Label'])
        self.assertFalse(service.path(spec['Label']).exists())
        self.assertTrue(any('enable' in argv for argv in calls))
        self.assertTrue(any('disable' in argv and spec['Label']+'.timer' in argv for argv in calls))
        self.assertTrue(all(argv[1]=='--user' for argv in calls))


    def test_minimal_up_down_through_real_fake_systemctl(self):
        from coordkit.manifest import load
        target=self.root/'minimal.toml'
        with contextlib.redirect_stdout(io.StringIO()):
            lifecycle.initialize('minimal',target,'anx',None,'owner/repo',profile='minimal')
        cfg=load(target); cfg.services.manager='systemd'
        with patch.dict(os.environ,SYSTEMCTL=str(self.root/'fakes/systemctl'),FAKE_SYSTEMD_STATE=str(self.root/'systemd-calls.jsonl')),contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(lifecycle.up(cfg,apply=True),0,output.getvalue())
            self.assertEqual(lifecycle.installation_drift(cfg),[])
            self.assertEqual(lifecycle.down(cfg),0)
        self.assertFalse(list((self.root/'config/systemd/user').glob('*')))
