# coordkit

**Agent-native local message bus for agent swarms.** Run coding agents across many machines with a shared message protocol, explicit recipients, acknowledgements, and durable handoffs.

coordkit is a small Python core with optional adapters. The bus is append-only JSON Lines on local disk. An optional self-hosted relay replicates rows across machines using outbound WebSockets; without `[relay]`, the bus stays local. The protocol is neutral across Claude Code, Codex, Hermes (ACP), and agentctl. v0.3 adds relay transport alongside Claude Code hooks, a tmux answer adapter, agentctl/ANX integration, and an optional MCP connector; it does not install Codex or Hermes hooks.

## Requirements and installation

Python **3.11+**, macOS or Linux. Runtime dependencies: Python's standard library only. Windows is unsupported (`fcntl` and POSIX process groups are used).

From a clone (no package installation needed):

```sh
git clone https://github.com/Git-on-my-level/coordkit.git
cd coordkit
export PYTHONPATH="$PWD/src"
python3 -m coordkit.cli --version
```

Or install the clone into a virtual environment with `python3 -m venv .venv && .venv/bin/python -m pip install .`, then use `.venv/bin/coordkit`. Release wheels can be installed with `python3 -m pip install /path/to/coordkit-0.3.0-py3-none-any.whl`. There is no PyPI release yet.

## Quickstart: an offline local bus

Run from the clone after setting `PYTHONPATH` above. No accounts, external CLIs, or services are needed. This creates only a disposable example directory.

```sh
mkdir -p .coordkit/quickstart
cat > .coordkit/quickstart/coordkit.toml <<'TOML'
[project]
name = "demo"
git_repo = "example/demo"
TOML
python3 -m coordkit.cli --manifest .coordkit/quickstart/coordkit.toml bus post \
  --from worker-a --to all --kind msg "Ready for work"
python3 -m coordkit.cli --manifest .coordkit/quickstart/coordkit.toml bus tail
```

ANX and agentctl configuration are optional. This minimal manifest runs without either tool. Remove `.coordkit/quickstart` to undo it.

For agent delivery, run `bus wait --to NAME --timeout 1` in one terminal and post to that reader from another. `--timeout` is minutes. Use `--force` when posting to a new unregistered reader, or declare it in `[addressed.agents.NAME]`. Use `bus ack ID --from NAME done` to acknowledge work.

## Optional integrations

| Feature | Prerequisites |
| --- | --- |
| Local bus, history, acknowledgements | Python only; external adapters are optional |
| Durable cards, leases, question escalation | `anx`, configured workspace/board/card |
| Managed lanes and usage | `agentctl`, configured pools |
| PR/CI observations | `gh` authenticated for the configured repository |
| Claude Code lead/question hooks | Claude Code configuration, `perl` for bounded hook execution |
| Claude lane moves | Usable destination account; `agent-heartbeat` for the default restart recipe |
| Deaf-agent answer delivery | Optional `tmux` adapter or `[questions].delivery_cli` |
| Scheduled `up` / `down` lifecycle | macOS launchd or Linux systemd user services |
| Remote MCP access | Explicit bearer secret, loopback server, operator-managed TLS tunnel |
| `[rc_host]` | Optional deployment-specific `claude-host-setup`; disabled by default |

`init PROJECT --git-repo OWNER/REPO` creates a minimal local installation. Add `--profile anx --workspace WORKSPACE` to provision ANX resources. Configure `[escalation].card` and `[launchd].path`, then run `doctor` and `up --plan` before `up --apply`. `down` removes managed jobs and restores receipted hooks. Review the [example manifests](examples/live/coordkit.toml) and [cutover pattern](docs/CUTOVER.md) before applying adapters.

## v0.2 delivery and updates

Live registered lanes wake automatically on addressed non-FYI posts when they are
not listening. Agentctl resumes the latest labelled execution; a burst gets one
wake per lane per five minutes by default. Retire a lane with `coordkit retire
NAME --to OWNER`; undo with `coordkit unretire NAME`. Claude Code sessions can
attach receipted delivery hooks with `coordkit hooks install`.

Use `coordkit update --check` for release notifications. The managed installer
(`scripts/install.sh`) supports checksum-verified updates, atomic release swaps,
hook reconciliation and rollback. Normal venv installs keep using their package
manager. See the [v0.2 contracts and operations guide](docs/V02.md) and
[generated operation reference](docs/OPS.md).

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

For buses across machines, see the optional [self-hosted relay](docs/RELAY.md).

## LANE-MOVE

`coordkit lane-move NAME --to-config DIR --plan` prepares a durable local plan for
moving a declared Claude lane to another configuration directory. Omitting both
mode flags also plans. Inspect the receipt under
`<paths.root>/state/lane-moves/lane-move-NAME-TIMESTAMP.json`, then rerun with
`--apply` to advance it. Repeating the same lane and destination resumes the
existing move; a different destination refuses until that move is finished.
Planning never posts to the bus or changes either account or the manifest.

```sh
coordkit lane-move worker --to-config /path/to/new-account --plan
coordkit lane-move worker --to-config /path/to/new-account --apply
```

Apply uses this sequence:

1. Write an editable `<receipt>.move-state.json` inventory and print its path, then
   post a freeze ask: finish the current turn and take no new work. The lane fills
   the handoff fields and every restart recipe, sets `inventory_complete=true`,
   acknowledges the ask, and picks up every open obligation it owns.
   `obligations.report` is checked without an age cutoff; any remaining obligation
   refuses the move. The freeze is a cooperative protocol; callers must stop
   assigning new work while the move is pending.
2. After recording every background shell, the operator runs `/exit` and chooses
   **"Exit and stop tasks" (stop background shells)** at Claude's three-way prompt
   (stop / background / stay). Poll until `claude_resume.holder_present` is false and the exact session's
   transcripts have been idle for `--idle-seconds` (default 10). Probe errors fail
   closed. Record descendant PID/start-time identities before exit; a lingering
   background-shell child blocks quiescence even after its parent exits and the
   child is reparented. Coordkit **never kills or signals** a process, and there is no
   `--stop-command`. A holder timeout posts an attention ask and leaves the plan
   resumable. `--timeout` bounds each holder/re-arm wait in seconds (default 60).
3. Finalize the move-state file and snapshot it in the receipt. It contains `goal`,
   `open_prs`, `lanes_in_flight` (including execution IDs), `waiters` (each name and
   flags), `bus_name`, `next_steps`, session/transcript paths, `background_shells`,
   and `re_arms`. Post `kind=state subject=lane-move state=DONE` with its path, session
   id and source transcript, then verify absence again.
4. Copy the session JSONL into the matching `projects/<encoded-cwd>/` directory
   under the new account. `--apply` authorizes this copy: the original is retained,
   the checksum is receipted before copying, and both copies must match. An
   existing different destination refuses; it is never overwritten. Only the
   parent session JSONL is copied; account settings, credentials and subagent
   directories are not migrated. The destination account must already be usable.
5. Back up the manifest beside the plan as `.manifest.bak`, receipt its before and
   after hashes, then atomically update only the lane's `config_dir` and
   `session_id`. The target lane table is reserialized; other tables and unrelated
   values are preserved. A concurrent change to the lane refuses. Unusual TOML
   that cannot be isolated safely also refuses without editing it.
6. Print the exact quoted operator command with `cd`, `CLAUDE_CONFIG_DIR`, `COORDKIT_AS` and
   `claude --resume SESSION`. By default the operator runs it,
   then reruns `--apply`. Only `resume="headless"` explicitly allows an automatic
   headless resume. Launch intent is saved first; a crash with an uncertain launch
   requires operator inspection and re-arm, never an automatic second launch.
7. Verify fresh destination resume evidence, then **arm and verify every recorded
   re-arm entry**. The default recipes start a bus waiter for the lane name with
   `--own-asks` (any kind of reply linked with `--re` to the lane's own asks, plus
   normal addressed wake kinds), and `agent-heartbeat 50` with the session's explicit
   heartbeat key. Include every extra waiter, monitor, watcher and background shell:
   they die with the old session. Each launch intent, PID/start time, output log,
   verification result and error is recorded in `rearm_outcomes`. A generic
   `listening()` result cannot substitute for any entry. Missing evidence posts an
   attention ask and leaves the move resumable; uncertain spawns never repeat.
   `--no-rearm` is the operator exception: skip automatic starts, record each skip,
   and still verify each manually armed entry.
8. Post a handoff with before/after `usage.collect` readings and roster identity
   in the receipt: configuration directory, account directory name and observed
   model. Account names follow the roster convention; these are not billing or
   authentication probes. Batch C is out of scope.

Phase bus publications use stable `lane-move-NAME-TIMESTAMP-PHASE` IDs; the final
handoff uses `lane-move-NAME-TIMESTAMP`. Each successful phase also has a
`lane.event` row. Repeating a finished move is a no-op. Retain the plan, backup and
resume log as private audit evidence; no cleanup or rollback runs automatically.
For rollback, quiesce the resumed lane first and review the backup against any
later manifest edits before restoring the original entry; the source JSONL is
still present. Receipts contain private paths and identity and must not be posted
to public repositories.

Restart recipes in `re_arms` have a unique `name`, a `kind` (`bus-waiter`,
`heartbeat`, `monitor`, `watcher`, or `background-shell`), and an `argv` array with
the exact command and flags. Optional `env` values apply to that recipe; optional
`verify_argv` runs a bounded readiness check in addition to PID/start-time liveness.
Heartbeat recipes require `verify_argv`, whose JSON status must report `event=arm`
and the same owner PID. Bus waiters require their exact recorded flags and, for
lane waiters, a freshly written cursor. Use foreground, bounded commands; move
receipts retain each process's output log for the resumed lane/operator to collect.
Move-state `background_shells` maps each observed child `pid` to a recipe's `rearm`
name, or an explicit `completed_reason` for a task that needs no restart. Confirm
unobserved/previously exited tasks in the inventory too. `waiters` is derived from
the recipes when final state is written, so its flags stay consistent.

With `--no-rearm`, add the manually armed process's `pid` and `started` (the
normalized `ps -o lstart=` value) to each file entry before rerunning apply. These
fields may be updated after resume; restart recipes are frozen in the receipt.
An uncertain automatic spawn requires inspecting the recorded PID/log before any
manual recovery. Coordkit never stops those processes or retries an ambiguous arm.
