#!/usr/bin/env python3
"""Opt-in macOS SessionStart probe: isolated homes, denied network, no prompt.

Run with PYTHONPATH=src, --claude PATH --scratch-dir PATH --output PATH.
Never reads existing native sessions or credentials. All temporary native handles
stay private and are deleted. Results contain version/hash and booleans only.
Manual Bash sourcing is not a model-driven tool turn or a live deployment gate.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile

from coordkit import deliver
from coordkit.manifest import load

DEADLINE = 30
CAPTURE = '''import json,os,sys,subprocess
raw=sys.stdin.buffer.read(32769)
p=json.loads(raw.decode('utf-8'))
v={'session':p.get('session_id'),'event':p.get('hook_event_name'),
   'env_file':os.environ.get('CLAUDE_ENV_FILE')}
fd=os.open(sys.argv[1],os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
with os.fdopen(fd,'w') as out: json.dump(v,out)
if len(sys.argv)>2:
 result=subprocess.run(sys.argv[2:],input=raw,timeout=5)
 raise SystemExit(result.returncode)
'''
FAKE_AGENTCTL = '''import json,os,sys
from pathlib import Path
v=json.loads(Path(sys.argv[1]).read_text())
if os.environ.get('PROBE_EXPECT_MANAGED'):
 print(json.dumps({'provider_absent':'AGENTCTL_CALLER_PROVIDER' not in os.environ,
  'session_absent':'AGENTCTL_CALLER_NATIVE_SESSION_ID' not in os.environ,
  'managed_preserved':os.environ.get('AGENTCTL_EXECUTION_ID')=='probe-managed'}))
else:
 print(json.dumps({'provider_matches':os.environ.get('AGENTCTL_CALLER_PROVIDER')=='claude',
  'session_matches':os.environ.get('AGENTCTL_CALLER_NATIVE_SESSION_ID')==v['session']}))
'''


def bounded(argv, *, env, cwd, timeout=DEADLINE):
    """Reap the exact process group on success, error or deadline."""
    with tempfile.TemporaryFile(dir=cwd) as out, tempfile.TemporaryFile(dir=cwd) as err:
        process = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                                   stdout=out, stderr=err, start_new_session=True)
        try:
            process.wait(timeout=timeout)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        out.seek(0)
        err.seek(0)
        stdout, stderr = out.read(16385), err.read(16385)
        if len(stdout) > 16384 or len(stderr) > 16384:
            raise RuntimeError('probe_output_bound')
        return process.returncode, stdout, stderr


def isolated(root):
    root.mkdir(mode=0o700)
    for name in ('home', 'config', 'cwd', 'tmp'):
        (root/name).mkdir(mode=0o700)
    return {'HOME': str(root/'home'), 'CLAUDE_CONFIG_DIR': str(root/'config'),
            'TMPDIR': str(root/'tmp') + '/', 'PATH': os.defpath,
            'LANG': 'en_US.UTF-8', 'ANTHROPIC_API_KEY': 'probe-not-a-real-credential',
            'DISABLE_AUTOUPDATER': '1', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
            'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--claude', required=True)
    parser.add_argument('--scratch-dir', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    if sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file():
        parser.error('requires macOS sandbox-exec; no unsandboxed fallback')
    binary, python = Path(args.claude).resolve(), Path(sys.executable).resolve()
    report = {'measured_at': dt.datetime.now(dt.timezone.utc).isoformat(),
              'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
              'hook_python_version': sys.version.split()[0], 'cases': []}
    with tempfile.TemporaryDirectory(prefix='coordkit-sessionstart-', dir=args.scratch_dir) as temporary:
        root = Path(temporary).resolve()
        profile = root/'deny.sb'
        # Native binary and Python runtime only: no real account/config/keychain reads.
        permitted = [binary.parent, Path(sys.base_prefix).resolve()]
        profile.write_text('(version 1)\n(allow default)\n(deny network*)\n'
                           '(deny file-read* (require-all (subpath ' + json.dumps(str(Path.home())) + ') '
                           '(require-not (require-any ' + ' '.join('(subpath ' + json.dumps(str(p)) + ')'
                                                                  for p in permitted) + '))))\n'
                           '(deny file-write* (subpath ' + json.dumps(str(Path.home())) + '))\n'
                           '(deny file-read* (subpath "/Library/Keychains"))\n'
                           '(deny file-read* (subpath "/Library/Application Support/ClaudeCode") '
                           '(subpath "/Library/Managed Preferences"))\n'
                           '(deny mach-lookup (global-name "com.apple.securityd") '
                           '(global-name "com.apple.securityd.xpc"))\n')
        sandbox = ['/usr/bin/sandbox-exec', '-f', str(profile)]
        probe_env = isolated(root/'network-check')
        code, _, error = bounded([*sandbox, str(python), '-c',
                                 'import socket; s=socket.socket(); s.connect(("127.0.0.1",9))'],
                                env=probe_env, cwd=root, timeout=5)
        if code == 0 or b'Operation not permitted' not in error:
            raise RuntimeError('network_deny_not_verified')
        report['network_deny_verified'] = True
        code, version, _ = bounded([*sandbox, str(binary), '--version'], env=probe_env, cwd=root, timeout=5)
        if code != 0 or len(version) > 100:
            raise RuntimeError('native_version_unavailable')
        report['binary_version'] = version.decode('utf-8').strip()
        observed_sessions = []
        for number in range(2):
            # Observe a real native session first; never invent the manifest pin.
            observed = None
            result = {}
            for phase in ('capability', 'bridge'):
                work = root/f'{number}-{phase}'
                env = isolated(work)
                capture, receipt = work/'capture.py', work/'native.private.json'
                capture.write_text(CAPTURE)
                settings = work/'settings.json'
                plugin_receipt = receipt if phase == 'capability' else work/'plugin.private.json'
                settings.write_text(json.dumps({'enabledPlugins': {}, 'hooks': {'SessionStart': [
                    {'hooks': [{'type': 'command', 'command': shlex.join([str(python), str(capture), str(plugin_receipt)]),
                                'timeout': 5}]}]}}))
                argv = [*sandbox, str(binary), '--init-only', '--setting-sources', '', '--strict-mcp-config',
                        '--mcp-config', '{"mcpServers":{}}', '--settings', str(settings)]
                if phase == 'bridge':
                    file = work/'coordkit.toml'
                    file.write_text('[project]\nname="native-probe"\ngit_repo="owner/repo"\n'
                                    '[launchd]\npython=' + json.dumps(str(python)) + '\n'
                                    '[[lanes]]\nname="probe"\nconfig_dir="config"\ncwd="cwd"\n'
                                    'model="test"\neffort="test"\nsession_id=' + json.dumps(observed['session']) + '\n')
                    deliver.install(load(file), settings)  # isolated fixture only; no live hooks
                    # Claude gives each SessionStart command its own env handle.
                    # Capture this command's handle, then run its exact generated
                    # argv with the untouched native stdin. The unrelated hook's
                    # handle cannot stand in for the bridge's private target.
                    installed = json.loads(settings.read_text())
                    bridge = installed['hooks']['SessionStart'][-1]['hooks'][0]
                    bridge['command'] = shlex.join([str(python), str(capture), str(receipt),
                                                    *shlex.split(bridge['command'])])
                    settings.write_text(json.dumps(installed))
                    argv += ['--session-id', observed['session']]
                code, _, error = bounded(argv, env=env, cwd=work/'cwd')
                print(json.dumps({'case': number + 1, 'phase': phase, 'native_exit_zero': code == 0}), flush=True)
                if code != 0:
                    if b'unknown option' in error and b'init-only' in error:
                        raise RuntimeError('init_only_unsupported')
                    raise RuntimeError('init_only_failed_no_prompt_fallback')
                current = json.loads(receipt.read_text()) if receipt.exists() else {}
                if (current.get('event') != 'SessionStart' or not current.get('session')
                        or not current.get('env_file')):
                    raise RuntimeError('native_sessionstart_handle_unavailable')
                result[phase + '_session_start'] = True
                if phase == 'capability':
                    observed = current
                    observed_sessions.append(current['session'])
                    continue
                if current['session'] != observed['session']:
                    raise RuntimeError('native_pin_mismatch')
                target = Path(current['env_file'])
                if not target.is_relative_to(work/'config'):
                    raise RuntimeError('native_target_outside_isolation')
                if not target.is_file():
                    raise RuntimeError('bridge_target_not_created')
                raw = target.read_bytes()
                unrelated = json.loads(plugin_receipt.read_text())
                result['unrelated_hook_preserved'] = unrelated.get('event') == 'SessionStart'
                result['distinct_per_hook_env_handles'] = unrelated.get('env_file') != current['env_file']
                result['matching_private_exports'] = (
                    b'export AGENTCTL_CALLER_PROVIDER=claude\n' in raw
                    and ('export AGENTCTL_CALLER_NATIVE_SESSION_ID=' + shlex.quote(current['session']) + '\n').encode() in raw)
                result['file_private'] = target.stat().st_mode & 0o777 == 0o600
                fake = work/'fake-agentctl.py'
                fake.write_text(FAKE_AGENTCTL)
                consumer = [*sandbox, '/bin/bash', '--noprofile', '--norc', '-c',
                            '. "$1"; exec "$2" "$3" "$4"', 'probe', str(target),
                            str(python), str(fake), str(receipt)]
                code, answer, error = bounded(consumer,
                                             env={'HOME': str(work/'home'), 'PATH': os.defpath}, cwd=work/'cwd', timeout=5)
                booleans = json.loads(answer) if code == 0 and not error else {}
                result['manual_bash_provider_matches'] = booleans.get('provider_matches') is True
                result['manual_bash_session_matches'] = booleans.get('session_matches') is True
                code, answer, error = bounded(consumer, env={'HOME': str(work/'home'), 'PATH': os.defpath,
                                             'AGENTCTL_EXECUTION_ID': 'probe-managed', 'PROBE_EXPECT_MANAGED': '1'},
                                             cwd=work/'cwd', timeout=5)
                booleans = json.loads(answer) if code == 0 and not error else {}
                result['managed_context_preserved_at_manual_source'] = all(
                    booleans.get(key) is True for key in ('provider_absent', 'session_absent', 'managed_preserved'))
            if not all(result.values()):
                raise RuntimeError('bridge_boolean_failed')
            report['cases'].append(result)
        report['distinct_native_sessions'] = len(set(observed_sessions)) == 2
        report['manual_sourcing_only'] = True
        report['provider_prompt_submitted'] = False
        report['process_groups_reaped'] = True
    report['temporary_contexts_removed'] = True
    Path(args.output).write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # No native stderr, session, env path or credential-bearing diagnostics.
        code = type(exc).__name__
        if isinstance(exc, RuntimeError) and str(exc) in {
                'probe_output_bound', 'network_deny_not_verified', 'native_version_unavailable',
                'init_only_unsupported', 'init_only_failed_no_prompt_fallback',
                'native_sessionstart_handle_unavailable', 'native_pin_mismatch',
                'native_target_outside_isolation', 'bridge_target_not_created', 'bridge_boolean_failed'}:
            code = str(exc)
        elif isinstance(exc, subprocess.TimeoutExpired):
            code = 'native_init_only_deadline' if '--init-only' in exc.cmd else 'local_check_deadline'
        print('SESSIONSTART_PROBE_FAILED ' + code, file=sys.stderr)
        raise SystemExit(1)
