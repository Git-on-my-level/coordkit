"""Optional adapters, discovered from binaries, manifest and entry points.

Core owns log/state/locks. Adapters may observe committed rows, report listening,
resume a lane, or project a row; no third-party packages are required.
"""
import os
import shutil
import warnings


def __getattr__(name):
    if name in ('HarnessAdapter', 'LauncherAdapter', 'MirrorAdapter'):
        from . import contracts
        return getattr(contracts, name)
    raise AttributeError(name)


SINKS = []  # In-process extension seam. No relay implementation is shipped.


def available(manifest):
    from .builtin import Agentctl, Anx, ClaudeCode, ReportOnly
    found = [ClaudeCode(), ReportOnly('codex', '.codex/config.toml'), ReportOnly('hermes-acp', '.hermes')]
    if shutil.which(os.environ.get('AGENTCTL', 'agentctl')):
        found.append(Agentctl())
    if manifest.anx.workspace and shutil.which(os.environ.get('ANX', 'anx')):
        found.append(Anx())
    # Explicit entry point names are optional; absent plugins do not poison core reads.
    from importlib import metadata
    selected = manifest.adapters
    for ep in (metadata.entry_points(group='coordkit.adapters') if selected else ()):
        if ep.name in selected:
            try:
                adapter = ep.load()()
                found.append(adapter)
            except Exception as exc:
                warnings.warn(f'adapter_unavailable: {ep.name}: {exc}', RuntimeWarning)
    return found


def get(manifest, name):
    return next((a for a in available(manifest) if a.name == name), None)


def notify(manifest, recs):
    """Post-append sinks run outside flock; failures cannot undo a durable post."""
    if not recs:
        return
    from ..wake import notify as wake
    sinks = [wake, *[s.notify for s in SINKS],
             *[a.notify for a in available(manifest) if callable(getattr(a, 'notify', None))]]
    for sink in sinks:
        try:
            sink(manifest, recs)
        except Exception as exc:
            warnings.warn(f'post_append_sink_failed: {type(exc).__name__}: {exc}', RuntimeWarning)
