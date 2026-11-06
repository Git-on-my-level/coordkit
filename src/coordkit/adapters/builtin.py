"""External-tool details stay in adapters, imported only when used."""
import json
import os
import shlex
from pathlib import Path
from ..process import ToolError, run


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
        from ..bus import label_matches
        # Exact lane lookup includes completed turns older than the liveness window.
        executions = self.client(manifest).recent(label=name)
        mine = [e for e in executions if any(label_matches(label, name) for label in e.get('labels', []))]
        if not mine:
            return {'status': 'unresumable', 'reason': 'no labelled agentctl execution; delivery hook required'}
        active = next((e for e in mine if e.get('state') in ('running', 'starting')), None)
        if active:
            return {'status': 'listening', 'execution': active['id']}
        latest = max(mine, key=lambda e: (e.get('created_at', ''), e.get('updated_at', ''), e.get('id', '')))
        prompt = (f'Coordkit wake for lane {name}. Read your whole inbox and handle pending addressed work.\n'
                  f'Run: coordkit --manifest {shlex.quote(str(manifest.file))} bus inbox {shlex.quote(name)} --full\n')
        result = run([self.client(manifest).binary, 'continue', latest['id'], '--request-key', key,
                      '--prompt-stdin', '--output', 'json'], stdin=prompt, timeout=30)
        if result.returncode:
            raise ToolError('agentctl', result.returncode, result.stderr + result.stdout)
        return {'status': 'woken', 'execution': latest['id'], 'result': json.loads(result.stdout)}


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
