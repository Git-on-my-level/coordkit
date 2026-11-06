# coordkit

**Agent-native local message bus for agent swarms.** Run coding agents across many machines with a shared message protocol, explicit recipients, acknowledgements, and durable handoffs.

coordkit is a small Python core with optional adapters. The bus is append-only JSON Lines on local disk. Agents on other machines reach the host through your existing transport; coordkit does not provide a distributed broker or bus replication. The protocol is neutral across Claude Code, Codex, Hermes (ACP), and agentctl. v0.1 includes Claude Code hooks, a tmux answer adapter, agentctl/ANX integration, and an optional MCP connector; it does not install Codex or Hermes hooks.

## Requirements and installation

Python **3.11+**, macOS or Linux. Runtime dependencies: Python's standard library only. Windows is unsupported (`fcntl` and POSIX process groups are used).

From a clone (no package installation needed):

```sh
git clone https://github.com/Git-on-my-level/coordkit.git
cd coordkit
export PYTHONPATH="$PWD/src"
python3 -m coordkit.cli --version
```

Or install the clone into a virtual environment with `python3 -m venv .venv && .venv/bin/python -m pip install .`, then use `.venv/bin/coordkit`. Release wheels can be installed with `python3 -m pip install /path/to/coordkit-0.1.0-py3-none-any.whl`. There is no PyPI release yet.

## Quickstart: an offline local bus

Run from the clone after setting `PYTHONPATH` above. No accounts, external CLIs, or services are needed. This creates only a disposable example directory.

```sh
mkdir -p .coordkit/quickstart
cat > .coordkit/quickstart/coordkit.toml <<'TOML'
[project]
name = "demo"
git_repo = "example/demo"
[anx]
workspace = "example"
board = "demo"
[agentctl.pools]
implement = "pool:implement"
[bus]
mirror = false
live_check = false
TOML
python3 -m coordkit.cli --manifest .coordkit/quickstart/coordkit.toml bus post \
  --from worker-a --to all --kind msg "Ready for work"
python3 -m coordkit.cli --manifest .coordkit/quickstart/coordkit.toml bus tail
```

The required ANX and pool fields satisfy the v0.1 manifest schema. Disabling mirroring and liveness checks makes this example fully local. Remove `.coordkit/quickstart` to undo it.

For agent delivery, run `bus wait --to NAME --timeout 1` in one terminal and post to that reader from another. `--timeout` is minutes. Use `--force` when posting to a new unregistered reader, or declare it in `[addressed.agents.NAME]`. Use `bus ack ID --from NAME done` to acknowledge work.

## Optional integrations

| Feature | Prerequisites |
| --- | --- |
| Local bus, history, acknowledgements | Python only; disable `[bus].mirror` and `live_check` for offline use |
| Durable cards, leases, question escalation | `anx`, configured workspace/board/card |
| Managed lanes and usage | `agentctl`, configured pools |
| PR/CI observations | `gh` authenticated for the configured repository |
| Claude Code lead/question hooks | Claude Code configuration, `perl` for bounded hook execution |
| Deaf-agent answer delivery | Optional `tmux` adapter or `[questions].delivery_cli` |
| Scheduled `up` / `down` lifecycle | macOS launchd; Linux users invoke CLI commands through their own scheduler |
| Remote MCP access | Explicit bearer secret, loopback server, operator-managed TLS tunnel |
| `[rc_host]` | Optional deployment-specific `claude-host-setup`; disabled by default |

`init PROJECT --workspace WORKSPACE --git-repo OWNER/REPO` provisions ANX resources; it is not the offline quickstart. Configure `[escalation].card` and `[launchd].path`, then run `doctor` and `up --plan` before `up --apply`. `down` removes managed jobs and restores receipted hooks. Review the [example manifests](examples/live/coordkit.toml) and [cutover pattern](docs/CUTOVER.md) before applying adapters.

## Concepts and contracts

- **Bus:** explicit sender, recipient, kind, topic, timestamp and ID; long bodies spill to ref files.
- **Obligations and addressed routing:** owners acknowledge handoffs and asks; roles choose listening members; overdue work reaches a fallback or digest.
- **Questions:** captured decisions climb from a live agent to a human; `[human-only]` forces the human tier.
- **Loops:** recurring work with cadence, receipts, and bounded failure escalation.
- **Shadow mode:** compares read-only protocol views before cutover; local observation state and relay output remain writable.

See [SPEC](docs/SPEC.md), [addressed routing](docs/ADDRESSED.md), [questions](docs/QUESTIONS.md), and [failure contracts](docs/FOOTGUNS.md). The [MCP connector](docs/MCP.md) is optional; read its [security model](docs/SECURITY.md) before exposing it.

## Development

```sh
python3 -m compileall -q src tests
PYTHONPATH=src python3 -m unittest discover
bash scripts/leak-check.sh
```

Tests use local fake CLIs; no external service credentials are required. MCP transport tests open loopback sockets. CI runs Python 3.11/3.12 on Linux and macOS with a five-minute job timeout. See [CONTRIBUTING](CONTRIBUTING.md).

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
