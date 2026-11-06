"""Console entry point. Milestones add their own subcommand parsers."""
import argparse
import sys
from pathlib import Path
from . import __version__
from .manifest import ManifestError, load
from .process import ToolError


def question_hook(argv):
    """`coordkit ... question hook EVENT`: a harness hook. It must never block a tool call: any bootstrap
    failure (missing or invalid manifest, import error) exits 0 with no decision."""
    try:
        parser = argparse.ArgumentParser(prog="coordkit")
        parser.add_argument("--manifest")
        parser.add_argument("command")
        options, rest = parser.parse_known_args(argv)
        from .questions import main as questions_main
        manifest = load(options.manifest)
        questions_main(rest, manifest)
    except BaseException:  # noqa: fail open, including SystemExit from argparse
        pass
    return 0


def main(argv=None):
    raw = sys.argv[1:] if argv is None else argv
    if 'deliver' in raw and '--hook' in raw:
        from .deliver import bootstrap
        return bootstrap(raw)
    if "question" in raw and raw[raw.index("question")+1:raw.index("question")+2] == ["hook"]:
        return question_hook(raw)
    parser = argparse.ArgumentParser(prog="coordkit")
    parser.add_argument("--manifest")
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    parser.add_argument("command", metavar="{bus,sweep,question,deliver,mcp-serve,doctor,up,down}", choices=["bus", "lane", "wait", "loop", "reconcile", "shadow", "obligations", "init", "up", "down", "doctor", "lead",
                                                "sweep", "review", "question", "mcp-serve", "deliver"])
    options, rest = parser.parse_known_args(argv)
    try:
        from .lifecycle import initialize, main as lifecycle_main, shadow_allowed
        if options.command == "init":
            init_parser = argparse.ArgumentParser(prog="coordkit init")
            init_parser.add_argument("project")
            init_parser.add_argument("--workspace", default="anx")
            init_parser.add_argument("--board")
            init_parser.add_argument("--git-repo")
            init_options = init_parser.parse_args(rest)
            target = Path(options.manifest or "coordkit.toml").resolve()
            if target.exists():
                load(target).writable()
                repo = init_options.git_repo or load(target).project.git_repo
            else:
                repo = init_options.git_repo
                if not repo:
                    raise ManifestError("init_requires_git_repo: pass --git-repo owner/name")
            return initialize(init_options.project, target, init_options.workspace, init_options.board, repo)
        manifest = load(options.manifest)
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
