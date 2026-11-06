"""Small service-manager adapters for launchd and systemd user services.

The interchange spec is a plain launchd-shaped dict. Only this module translates
it into manager-specific files and commands. No service is installed on import.
"""
import os
from pathlib import Path
import plistlib
import re
import shlex
import sys
from . import process
from .manifest import ManifestError
from .process import ToolError


def checked(argv):
    result = process.run(argv, timeout=120)
    if result.returncode:
        raise ToolError(argv[0], result.returncode, result.stderr + result.stdout)
    return result.stdout


class Launchd:
    name = 'launchd'
    def __init__(self, manifest):
        self.manifest = manifest

    def argv(self, *args):
        return [os.environ.get('LAUNCHCTL', 'launchctl'), *args]

    def target(self, label):
        return f'gui/{os.getuid()}/' + label

    def installed_labels(self, prefix):
        raw = checked(self.argv('list'))
        return sorted({line.split()[-1] for line in raw.splitlines()
                       if line.split() and line.split()[-1].startswith(prefix + '.')})

    def detail(self, label):
        return checked(self.argv('print', self.target(label)))

    def render(self, spec):
        return plistlib.dumps(spec)

    def path(self, label):
        return self.manifest.paths.root/'launchd'/(label + '.plist')

    def install(self, spec, previous=False):
        self.manifest.writable()
        path = self.path(spec['Label']); path.parent.mkdir(parents=True, exist_ok=True)
        if previous:
            self.uninstall(spec['Label'])
        path.write_bytes(self.render(spec))
        checked(self.argv('bootstrap', f'gui/{os.getuid()}', str(path)))
        return path

    def uninstall(self, label):
        self.manifest.writable()
        result = process.run(self.argv('print', self.target(label)))
        if result.returncode == 0:
            checked(self.argv('bootout', self.target(label)))
        elif 'Could not find service' not in result.stderr:
            raise ToolError('launchctl', result.returncode, result.stderr + result.stdout)

    def kick(self, label):
        self.manifest.writable()
        checked(self.argv('kickstart', '-k', self.target(label)))


# systemd uses its own quoting, not shell quoting. '%' specifiers and '$'
# environment expansion must be escaped even inside quoted strings.
def quote(value):
    value = str(value)
    if '\n' in value or '\r' in value or '\0' in value:
        raise ManifestError('invalid_service_value: newline or NUL')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


class Systemd:
    name = 'systemd'
    def __init__(self, manifest):
        self.manifest = manifest
        self.directory = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home()/'.config')))/'systemd/user'

    def argv(self, *args):
        return [os.environ.get('SYSTEMCTL', 'systemctl'), '--user', *args]

    def installed_labels(self, prefix):
        output = checked(self.argv('list-unit-files', '--no-legend', '--no-pager'))
        return sorted({line.split()[0].removesuffix('.service') for line in output.splitlines()
                       if line.split() and line.split()[0].startswith(prefix + '.') and line.split()[0].endswith('.service')})

    def detail(self, label):
        return checked(self.argv('show', label + '.service', '--property=ExecStart,Environment,FragmentPath,LoadState'))

    def path(self, label):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', label):
            raise ManifestError('invalid_service_label')
        return self.directory/(label + '.service')

    def render(self, spec):
        label = spec['Label']; self.path(label)
        env = ''.join('Environment=' + quote(k + '=' + v) + '\n' for k, v in sorted(spec.get('EnvironmentVariables', {}).items()))
        service = ('[Unit]\nDescription=Coordkit ' + label + '\n\n[Service]\n' + ('Type=oneshot\n' if spec.get('StartInterval') else 'Type=simple\nRestart=on-failure\n')
                   + 'WorkingDirectory=' + quote(spec['WorkingDirectory']) + '\n' + env +
                   'ExecStart=' + ' '.join(quote(a) for a in spec['ProgramArguments']) + '\n' +
                   'StandardOutput=append:' + quote(spec['StandardOutPath']) + '\n' +
                   'StandardError=append:' + quote(spec['StandardErrorPath']) + '\n')
        if spec.get('StartInterval'):
            timer = ('[Unit]\nDescription=Coordkit timer ' + label + '\n\n[Timer]\nOnBootSec=1s\n'
                     f'OnUnitActiveSec={spec["StartInterval"]}s\nPersistent=true\nUnit={label}.service\n\n'
                     '[Install]\nWantedBy=timers.target\n')
            return {label + '.service': service, label + '.timer': timer}
        return {label + '.service': service + '\n[Install]\nWantedBy=default.target\n'}

    def drift(self, spec):
        label = spec['Label']; problems = []
        for name, expected in self.render(spec).items():
            path = self.directory/name
            if not path.exists() or path.read_text() != expected:
                problems.append('unit drift ' + name)
        detail = self.detail(label)
        if 'LoadState=not-found' in detail:
            problems.append('missing loaded unit ' + label)
        for arg in spec['ProgramArguments']:
            # systemctl show escapes whitespace; file comparison remains exact.
            if arg not in detail and arg.replace(' ', '\\x20') not in detail:
                problems.append('loaded arguments drift ' + label)
                break
        for key, value in spec.get('EnvironmentVariables', {}).items():
            if key + '=' + value not in detail and key + '=' + value.replace(' ', '\\x20') not in detail:
                problems.append('loaded environment drift ' + label)
                break
        return problems

    def install(self, spec, previous=False):
        self.manifest.writable()
        label = spec['Label']
        if previous:
            self.uninstall(label)
        self.directory.mkdir(parents=True, exist_ok=True)
        for name, text in self.render(spec).items():
            (self.directory/name).write_text(text)
        checked(self.argv('daemon-reload'))
        suffix = '.timer' if spec.get('StartInterval') else '.service'
        checked(self.argv('enable', '--now', label + suffix))
        return self.path(label)

    def uninstall(self, label):
        self.manifest.writable()
        path = self.path(label)
        timer = path.with_suffix('.timer')
        # Disable both: the timer can otherwise start a service after it is stopped.
        for unit in (timer, path):
            result = process.run(self.argv('disable', '--now', unit.name))
            if result.returncode and not any(s in result.stderr for s in ('does not exist', 'not loaded', 'not found')):
                raise ToolError('systemctl', result.returncode, result.stderr + result.stdout)
            unit.unlink(missing_ok=True)
        checked(self.argv('daemon-reload'))

    def kick(self, label):
        self.manifest.writable()
        checked(self.argv('restart', self.path(label).name))


def manager(manifest):
    choice = manifest.services.manager or ('launchd' if sys.platform == 'darwin' else 'systemd' if sys.platform.startswith('linux') else '')
    if choice not in ('launchd', 'systemd'):
        raise ManifestError('unsupported_service_manager: ' + choice)
    return (Launchd if choice == 'launchd' else Systemd)(manifest)
