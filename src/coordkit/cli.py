"""Console entry point. Milestones add their own subcommand parsers."""
import argparse
import sys
from pathlib import Path
from . import __version__


def question_hook(argv):
    """`coordkit ... question hook EVENT`: a harness hook. It must never block a tool call: any bootstrap
    failure (missing or invalid manifest, import error) exits 0 with no decision."""
    try:
        parser = argparse.ArgumentParser(prog="coordkit")
        parser.add_argument("--manifest")
        parser.add_argument("command")
        options, rest = parser.parse_known_args(argv)
        from .questions import main as questions_main
        from .manifest import load
        manifest = load(options.manifest)
        questions_main(rest, manifest)
    except BaseException:  # noqa: fail open, including SystemExit from argparse
        pass
    return 0


def main(argv=None):
    raw = sys.argv[1:] if argv is None else argv
    if 'deliver' in raw and '--stop-hook' in raw:
        try:
            from .hook_runtime import hook_deadline
            with hook_deadline():
                from .deliver import bootstrap
                return bootstrap(raw)
        except BaseException:
            return 0
    if 'deliver' in raw and '--hook' in raw:
        from .deliver import bootstrap
        return bootstrap(raw)
    if "question" in raw and raw[raw.index("question")+1:raw.index("question")+2] == ["hook"]:
        return question_hook(raw)
    from .manifest import ManifestError, load
    from .process import ToolError
    parser = argparse.ArgumentParser(prog="coordkit")
    parser.add_argument("--manifest")
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    from .ops import COMMANDS
    parser.add_argument("command", choices=tuple(COMMANDS))
    options, rest = parser.parse_known_args(argv)
    try:
        if options.command == 'update':
            from .selfupdate import main as update_main
            return update_main(rest, load(options.manifest) if options.manifest else None)
        if options.command == 'bus':
            from .bus import main as bus_main
            return bus_main(rest, load(options.manifest))
        if options.command == 'sessions':
            from .sessions import main as sessions_main
            return sessions_main(rest)
        if options.command in ('ops', 'version'):
            if options.command == 'version':
                print(__version__)
                return 0
            from .ops import main as ops_main
            return ops_main(rest)
        from .lifecycle import initialize, main as lifecycle_main, shadow_allowed
        if options.command == "init":
            from .ops import parser_for
            init_parser = parser_for('init')
            init_options = init_parser.parse_args(rest)
            target = Path(options.manifest or "coordkit.toml").resolve()
            if target.exists():
                load(target).writable()
                repo = init_options.git_repo or load(target).project.git_repo
            else:
                repo = init_options.git_repo
                if not repo:
                    raise ManifestError("init_requires_git_repo: pass --git-repo owner/name")
            return initialize(init_options.project, target, init_options.workspace, init_options.board, repo, profile=init_options.profile)
        manifest = load(options.manifest)
        if options.command in ('retire', 'unretire'):
            from .ops import parser_for
            lane_parser = parser_for(options.command)
            lane_options = lane_parser.parse_args(rest)
            from .wake import change
            return change(manifest, lane_options.lane, getattr(lane_options, 'to', None), undo=options.command == 'unretire')
        if options.command == 'wake-scan':
            from .ops import parser_for
            from .wake import scan
            return scan(manifest, dry_run=parser_for('wake-scan').parse_args(rest).dry_run)
        if options.command == 'wake-helper':
            from .peerwake import main as helper_main
            return helper_main(rest, manifest)
        if options.command == 'relay':
            from .relay import main as relay_main
            return relay_main(rest, manifest)
        if options.command == 'mcp':
            from .mcp_install import main as mcp_install_main
            return mcp_install_main(rest, manifest)
        if options.command == 'hooks':
            from .hooks import main as hooks_main
            return hooks_main(rest, manifest)
        if options.command == 'deliver':
            from .deliver import main as deliver_main
            return deliver_main(rest, manifest)
        if manifest.mode == "shadow" and not shadow_allowed(options.command, rest):
            manifest.writable()
        if options.command == "sweep":
            from .addressed import main as addressed_main
            return addressed_main(rest, manifest, options.command)
        if options.command == "mcp-serve":
            from .mcp_server import main as mcp_main
            return mcp_main(rest, manifest)
        if options.command in ("up", "down", "doctor", "lead"):
            return lifecycle_main(rest, manifest, options.command)
        if options.command in ("bus", "loop", "lane", "wait", "reconcile", "shadow", "obligations", "review", "question"):
            module = __import__(f"coordkit.{options.command if options.command != 'question' else 'questions'}", fromlist=["main"])
            return module.main(rest, manifest)
        raise ManifestError(f"not_implemented: {options.command}")
    except (ManifestError, ToolError) as exc:
        print(str(exc), file=sys.stderr)
        return 1 if options.command == "doctor" else exc.exit_code
    except (OSError, ValueError) as exc:
        print(f"coordkit_error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
