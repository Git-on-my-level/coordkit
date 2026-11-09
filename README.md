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
- **Loops:** recurring work with cadence, receipts, and repeated failure asks in ANX Decide. See [recovery and mirror replay](docs/LOOP-RECOVERY.md).
- **Shadow mode:** compares read-only protocol views before cutover; local observation state and relay output remain writable.

Coordination safeguards apply by default. Unchanged asks reuse one durable question;
explicit defaults and ownerless approvals converge through the
[question policy](docs/QUESTIONS.md#unanswered-asks-converge-by-default).
Per-item opt-outs cover exceptional asks and approvals.

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
If that directory overlaps any `mcp.ref_roots`, private receipts instead live under
`~/.local/state/coordkit/private-lane-moves/<manifest-hash>/`. A configuration that
also exposes this fallback refuses before writing private state. Plans and inventory
files are always mode 0600; do not put them in ref roots or copy them onto the bus.
Existing exposed artifacts refuse migration until the operator relocates them;
changing ref roots must never silently hide an unfinished receipt.

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
   resumable. `--timeout` bounds each holder wait, destination resume verification,
   and re-arm polling separately in seconds (default 60 for each stage).
3. Finalize the move-state file and snapshot it in the receipt. It contains `goal`,
   `open_prs`, `lanes_in_flight` (including execution IDs), `waiters` (each name and
   flags), `bus_name`, `next_steps`, session/transcript paths, `background_shells`,
   `subscriptions`, and `re_arms`. Subscription inventory records execution ID,
   original subscription ID, source host/authority, host-local event target, kinds,
   expiry and the renewal TTL (72h). Distinct event-kind filter sets on the same
   execution are separate inventory and outcome keys; all are recreated. Include every execution in `lanes_in_flight`;
   missing callbacks refuse finalization. Post a redacted final-state summary,
   then verify absence again.
4. Copy the session JSONL into the matching `projects/<encoded-cwd>/` directory
   under the new account. `--apply` authorizes this copy: the original is retained,
   the checksum is receipted before copying, the copy streams in 1 MiB chunks,
   and both copies must match. An
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
7. Verify a fresh hook note with the exact destination transcript and session;
   a waiter or cursor refresh never proves resume. Then **arm and verify every recorded
   re-arm entry**. The default recipes start a bus waiter for the lane name with
   `--persistent --timeout 0 --own-asks` (any kind of reply linked with `--re` to the lane's own asks, plus
   normal addressed wake kinds), and `agent-heartbeat 50` with the session's explicit
   heartbeat key. Include every extra waiter, monitor, watcher and background shell:
   they die with the old session. Persistent waiters continue consuming after each
   match, flush and fsync deliveries to regular receiver logs before committing
   the cursor. Before each append, they check the last 1 MiB of the receiver log
   for newline-terminated `-- end <id>` completion markers, skipping delivered
   IDs and advancing the cursor. Both normal and `--full` deliveries append this
   marker after the body in the same buffered write. Body lines matching the
   marker syntax gain one leading space so text cannot forge completion.
   A header without its marker is replayed, with a separating newline after any
   unterminated fragment; legacy
   logs without markers are replayed once. This makes delivery effectively once
   across a crash between receiver fsync and cursor commit, provided the marker remains in that
   bounded tail. Append to the same readable receiver log across restarts; pipes and terminals
   cannot provide persistent deduplication. Waiters stay armed until explicitly
   stopped by the operator. This is a durable log receiver, not injection into Claude.
   A second consumer on the same lane cursor refuses; verification requires the
   exact PID, start time, session and cursor-owner record. Each launch intent, PID/start time, output log,
   verification result and error is recorded in `rearm_outcomes`. A generic
   `listening()` result cannot substitute for any entry. Missing evidence posts an
   attention ask and leaves the move resumable; uncertain spawns never repeat.
   `--no-rearm` is the operator exception: skip automatic starts, record each skip,
   and still verify each manually armed entry. Recreate each inventoried execution
   subscription through destination agentctl, targeting the destination manifest's
   event file with a fresh 72h TTL, then verify its new ID, active state, host, target,
   kinds and expiry. Expired source subscriptions are renewed too. On a cross-host
   callback transfer, copy the source inventory's `subscriptions` into the destination
   move-state and use the destination manifest/event path; source-host receipts never prove destination
   readiness. The execution must be resolvable by the destination agentctl authority;
   otherwise the move stays incomplete for operator routing/transport repair. This
   command does not install a cross-host relay or mutate the source host. With
   `--no-rearm`, record each manually recreated subscription's `replacement_id` in
   the private inventory; it is checked against destination agentctl as well.
8. Post a handoff with before/after `usage.collect` readings and roster identity
   in the receipt: configuration directory, account directory name and observed
   model. Account names follow the roster convention; these are not billing or
   authentication probes. Batch C is out of scope.

Phase bus publications use stable `lane-move-NAME-TIMESTAMP-PHASE` IDs; the final
handoff uses `lane-move-NAME-TIMESTAMP`. Each successful phase also has a
`lane.event` row. The event writer newline-terminates an interrupted tail under
its lock, preserves the original bytes, and warns when malformed complete rows
are skipped. Phase completion follows event fsync and read-back verification;
retries deduplicate the receipt under the same lock.
Bus refs and handoff evidence contain an allowlisted redacted
summary only: move ID, lane, phase, completed phases and attention code. Private
paths, raw commands, environment values and process/session identities are excluded.
Paused version-1 receipts discard the old resume verification and require an
upgraded persistent-waiter/subscription inventory before any re-arm.
Repeating `--apply` after completion verifies the required lane waiter again.
A proven dead or reused PID is replaced and verified without replaying the copy,
resume, heartbeat or other completed arms. An uncertain spawn or still-live
unverified identity refuses automatic replacement; inspect it before manual recovery. Retain the plan, backup and
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
lane waiters, a freshly written cursor owned by that process and session. Waiter
recipes require `--persistent`. The required waiter addressed to the moved lane
must be unbounded: use `--timeout 0`, or omit timeout (persistent mode defaults to
unlimited). Finite timeouts, including equals syntax or later overrides, refuse.
It must also cover every default wake kind and accept replies to its own asks
(enabled by default). Narrowing `--kinds` or passing `--no-own-asks` refuses for
the required waiter. Additional waiters may filter kinds, disable own-ask replies,
or use a finite timeout. Both `--to NAME` and `--to=NAME` require the same fresh
cursor and process/session owner record. Readiness checks run once in a receipt-producing child;
`--timeout` bounds polling without killing a slow verifier. Rerun apply to collect
its result; an ambiguous verifier intent never launches a duplicate. Use foreground commands; move
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

Lane-move history scans refuse beyond 2048 directory entries, 128 receipts or a receipt over 1 MiB; archive
completed receipts outside the move directory before retrying. Usage readings scan
at most 128 files, 2048 entries per source tree and depth 8, reading a 2 MiB tail per
file and 16 MiB overall. `usage.truncated=true` marks incomplete readings. These
are recent-window observations, not lifetime accounting. Own-ask waiter seeding
uses the last 1 MiB of bus history and retains at most 4096 ask IDs; replies to
older excluded asks still wake when they match the normal addressed wake kinds.

Move obligation checks read a bus tail capped at 4 MiB. If the tail cannot cover
the retained history, the move refuses with `obligation_history_limit` rather than
silently overlooking old obligations; reconcile/archive history before retrying.
