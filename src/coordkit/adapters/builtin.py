"""External-tool details stay in adapters, imported only when used."""
import os
import shlex
import signal
import subprocess
import time
from pathlib import Path
from ..process import ToolError


RESUME_REFUSAL_SECONDS = 5


class Agentctl:
    name = 'agentctl'

    def client(self, manifest):
        from ..actl import AgentctlClient
        return AgentctlClient(manifest)

    def recent(self, manifest):
        return self.client(manifest).recent()

    def launch(self, manifest, *args, **kwargs):
        return self.client(manifest).delegate(*args, **kwargs)

    def cancel(self, manifest, execution):
        return self.client(manifest).cancel(execution)

    def await_result(self, manifest, execution):
        return self.client(manifest).await_result(execution)

    def owns(self, manifest, execution):
        return self.client(manifest).owns(execution)

    def listening(self, manifest, name, rows):
        from ..bus import label_matches
        return any(e.get('state') in ('running', 'starting') and
                   any(label_matches(label, name) for label in e.get('labels', []))
                   for e in self.client(manifest).recent(label=name, nonterminal=True))

    def wake(self, manifest, name, rows, key):
        from ..wake import WAKE_LABEL
        # Exact lane lookup includes completed turns older than the liveness window.
        executions = self.client(manifest).recent(label=WAKE_LABEL + name)
        mine = [e for e in executions if WAKE_LABEL + name in e.get('labels', [])]
        if not mine:
            return {'status': 'unresumable', 'reason': 'no ' + WAKE_LABEL + name + ' agentctl execution; delivery hook required'}
        active = next((e for e in mine if e.get('state') in ('running', 'starting')), None)
        if active:
            return {'status': 'listening', 'execution': active['id']}
        latest = max(mine, key=lambda e: (e.get('created_at', ''), e.get('updated_at', ''), e.get('id', '')))
        prompt = (f'Coordkit wake for lane {name}. Read your whole bus inbox (your usual inbox command) and handle pending addressed work.\n'
                  f'If you have none: coordkit --manifest {shlex.quote(str(manifest.file))} bus inbox {shlex.quote(name)} --full\n')
        # continue owns the resumed turn from its own process; a bounded run() here would
        # cancel the turn at the timeout. Start it in its own session and check only
        # for an immediate refusal.
        from ..wake import wake_path
        log = wake_path(manifest, name).with_suffix('.continue.log')
        with open(log, 'ab') as out:
            proc = subprocess.Popen([self.client(manifest).binary, 'continue', latest['id'], '--request-key', key,
                                     '--prompt-stdin', '--output', 'json'], stdin=subprocess.PIPE, stdout=out,
                                    stderr=out, start_new_session=True)
            proc.stdin.write(prompt.encode()); proc.stdin.close()
        deadline = time.time() + 5
        while proc.poll() is None and time.time() < deadline:
            time.sleep(0.2)
        if proc.returncode:
            raise ToolError('agentctl', proc.returncode, log.read_text(errors='replace')[-2000:])
        return {'status': 'woken', 'execution': latest['id'], 'pid': proc.pid}


class Anx:
    name = 'anx'
    def project(self, manifest, row, all_rows):
        from ..mirror import mirror
        return mirror(manifest, row, all_rows)


class ClaudeCode:
    name = 'claude-code'
    def listening(self, manifest, name, rows):
        from ..addressed import listening
        return listening(manifest, name, rows=rows)

    def wake(self, manifest, name, rows, key):
        from .. import addressed
        from ..state import read_json, write_json
        from . import claude_resume
        from ..wake import wake_path
        lane = next((lane for lane in manifest.lanes if lane.name == name
                     and lane.resume == 'headless' and lane.session_id), None)
        if lane is None:
            return {'status': 'unresumable', 'reason': 'no headless lane with a pinned session_id'}
        path = wake_path(manifest, name).with_suffix('.resume.pid')
        with claude_resume.session_lock(lane) as held:
            if held is None:
                return {'status': 'listening', 'evidence': 'resume in progress'}
            pid, legacy = claude_resume.read_pid(path)
            identity_path = path.with_suffix('.identity.json')
            try:
                record = read_json(identity_path, legacy)
            except (ValueError, UnicodeError):
                record = legacy
            if not isinstance(record, dict):
                record = legacy
            try:
                snapshot = claude_resume.processes()
                if claude_resume.holder_present(manifest, lane, snapshot):
                    return {'status': 'unresumable', 'reason': 'holder_present'}
                if pid and claude_resume.pid_matches(pid, record, snapshot):
                    return {'status': 'listening', 'evidence': 'headless resume running', 'pid': pid}
            except ToolError as exc:
                return {'status': 'unresumable', 'reason': exc.detail}
            except (OSError, ValueError, TypeError, subprocess.SubprocessError):
                return {'status': 'unresumable', 'reason': 'holder_probe_failed'}
            # Re-check after acquiring the resume lock: a waiter or an interactive
            # turn may have appeared since notify's policy check.
            for options in ({'rows': rows}, {'waiter_only': True}):
                live, evidence = addressed.listening(manifest, name, **options)
                if live:
                    return {'status': 'listening', 'evidence': evidence}
            prompt = (f'Coordkit wake for lane {name}. Read your whole bus inbox (your usual inbox command), '
                      'handle pending addressed work, and reply on the bus. Then end your turn with a '
                      f'background bus wait --to {shlex.quote(name)} armed.\n'
                      f'If you have no inbox command: coordkit --manifest {shlex.quote(str(manifest.file))} '
                      f'bus inbox {shlex.quote(name)} --full\n')
            env = os.environ.copy()
            if lane.config_dir:
                env['CLAUDE_CONFIG_DIR'] = lane.config_dir
            log = wake_path(manifest, name).with_suffix('.resume.log')
            # The session lock spans both holder checks and launch, even when
            # another manifest uses a different bus, lane name or state root.
            try:
                if claude_resume.holder_present(manifest, lane, claude_resume.processes()):
                    return {'status': 'unresumable', 'reason': 'holder_present'}
            except ToolError as exc:
                return {'status': 'unresumable', 'reason': exc.detail}
            except (OSError, ValueError, TypeError, subprocess.SubprocessError):
                return {'status': 'unresumable', 'reason': 'holder_probe_failed'}
            # The shell records its own PID before exec, closing the parent-crash
            # window. -e refuses to exec if the PID write fails. Precreate privately.
            path.parent.mkdir(parents=True, exist_ok=True)
            pid_fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            os.close(pid_fd)
            fd = os.open(log, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            with os.fdopen(fd, 'ab') as out:
                proc = subprocess.Popen(['/bin/sh', '-e', '-c', 'echo $$ > "$1"; shift; exec "$@"',
                                         'sh', str(path), 'claude', '--resume', lane.session_id, '-p', prompt],
                                        stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                        cwd=lane.cwd or None, env=env, start_new_session=True)
            try:
                write_json(identity_path, {'pid': proc.pid, 'started': claude_resume.start_time(proc.pid),
                                           'session_id': lane.session_id, 'key': key})
            except BaseException:
                # Refuse a controlled launch failure; abrupt parent death is covered
                # by the child-written PID and the holder probe on the next wake.
                try:
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                finally:
                    proc.wait()
                raise
            deadline = time.monotonic() + RESUME_REFUSAL_SECONDS
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(min(.2, max(0, deadline - time.monotonic())))
            if proc.poll():
                path.unlink(missing_ok=True)
                identity_path.unlink(missing_ok=True)
                with log.open('rb') as stream:
                    stream.seek(max(0, os.fstat(stream.fileno()).st_size - 2000))
                    detail = stream.read().decode(errors='replace')
                raise ToolError('claude', proc.returncode, detail)
            return {'status': 'woken', 'session_id': lane.session_id, 'pid': proc.pid}

    def install_hooks(self, manifest, target):
        from ..deliver import install
        return install(manifest, target)

    def remove_hooks(self, manifest, target):
        from ..deliver import uninstall
        return uninstall(manifest, target)

    def transcripts(self, manifest):
        return [lane.session_id for lane in manifest.lanes if lane.session_id]


class ReportOnly:
    def __init__(self, name, config):
        self.name, self.config = name, config

    def status(self):
        return 'report-only: ' + ('config present' if (Path.home()/self.config).exists() else 'absent (optional)')
