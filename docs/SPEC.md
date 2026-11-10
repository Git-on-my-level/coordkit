# coordkit v0.1 contract

coordkit is a manifest-driven, agent-native local message bus. Python 3.11+, standard-library runtime, POSIX filesystem locks and process groups. Complexity belongs in optional integrations. The local bus remains the record even when an integration is unavailable.

## Storage and delivery

The bus is append-only UTF-8 JSON Lines. Rows carry `id`, `ts`, `frm`, `to`, `kind`, `topic`, and `text`; older rows without IDs remain readable. Partial trailing appends are ignored until complete. File locking serializes writers. Large bodies spill to a ref file; readers can request full text. Ref files and cursors live under the configured bus state directory.

CLI `--full` and MCP `ref_read` share structured-ref validation under `[mcp].ref_roots`.
Text such as `[full: PATH]` never authorizes a read. Full text prefers `spill_ref`,
then `ref`; a missing, unreadable or disallowed ref leaves CLI inline text visible.

Kinds include messages, asks, approvals, rulings, claims, handoffs, blockers, decisions, acknowledgements, holds, state transitions, and structured signals. Replies use `re`. A ruling names one owner. An acknowledgement closes it, including a forced ack from a non-owner; a later direct reply of any kind from the owner also closes it. Automatic copies/escalations and bystander non-ack replies do not. A later ruling from the same sender to the same owner naming a shared PR supersedes the earlier ruling. Ownerless approvals/rulings publish with `owner_missing=true` and enter the hourly digest after 30 minutes unless exempted with `no_owner_required`. A later explicit owner or claim clears the topic finding. A blocker names an owner and what would unblock it; a later decision or answer closes it regardless of sender unless it is marked `auto_received`.

An owned ruling or blocker whose owner has an explicit retirement record, or whose owner has posted nothing for `[obligations].inactive_after`, is derived into a separate orphaned list rather than the open list. The default inactivity horizon is 72 hours. With no owner activity evidence, an old obligation uses its own timestamp and is marked `unknown`, not presumed live. The reconciler routes each orphan once to the retirement redirect, else the owner's addressed fallback, else the obligations coordinator (digest-only targets queue a lead finding instead of a bus ask). `[reconciler].orphan_ask_cap` bounds attempts per pass (default 10; 0 queues only a backlog summary); deferred or failed items retry without delivery receipts; `reconcile/state.json` keeps a per-id `orphan_notified` receipt even when other action receipts expire. Read the separate sets with `bus open-rulings --orphaned` and `bus open-blockers --orphaned`. Closure and orphaning never edit retained rows.

`bus state` retains the recorded `RUNNING` value but adds `effective_state=STALE` and `stale=true` when that lane has also been silent beyond `inactive_after`; an ordinary later post revives the derived running view. Holds are evaluated in log order, scoped to a PR and the authorized poster. Unrelated PR mentions and bystander replies cannot clear another agent's obligations.

`bus wait --to NAME` uses physical row cursors and the configured wake kinds. Structured signal/state waits match their fields rather than prose. A cursor proves scanning, not human reading or task completion. No bus merge, replication, or cross-host consensus is provided.

## Manifest

`coordkit.toml` is discovered by walking up from the working directory; `--manifest PATH` overrides discovery. Paths resolve relative to the manifest, with `~` expansion. Unknown keys and wrong types fail with `ManifestError`; durations use units such as `15m` or `2h`. The sole compatibility exception is `wake-scan`, which ignores unknown top-level **tables** so an unrelated new integration cannot disable wake delivery; unknown scalars and unknown keys inside known tables still fail. New manifests may declare top-level `minimum_coordkit_version = "MAJOR.MINOR.PATCH"`; every reader checks it first and fails with `manifest_requires_newer_coordkit` when too old, including tolerant wake scans. Use it whenever an older reader must not ignore a new table's semantics. The authoritative field list and defaults are dataclasses in `src/coordkit/manifest.py`.

Required even for a local-only v0.1 bus:

```toml
[project]
name = "sample"                 # lowercase slug
git_repo = "example/repository" # owner/name
[anx]
workspace = "example"
board = "sample"
[agentctl.pools]
implement = "pool:implement"
```

Optional configuration groups:

| Section | Contract |
| --- | --- |
| `paths` | `root` defaults to `.coordkit`; bus, events and run output default below it. Explicit file paths may be shared with an incumbent bus. |
| `bus` | `text_cap=400`, liveness probes and ANX mirroring enabled by default; disable both for a local-only bus. Aliases canonicalize recipients. |
| `agents` | Static roster of labels and ANX actors; `lead` is reserved. |
| `addressed` | Named agents, ordered role members, optional human roles, relay and deadlines. Only configured roles and built-in `lead` supply lead names; `relay` is special only when configured. `internal_senders=["coordkit","coordkit-report"]` suppresses normal internal pickup reminders, retaining urgent ones. Doctor roster thresholds are `roster_silent_after="72h"`, `roster_unenrolled_posts=20`, and `roster_sink_received=20`. |
| `obligations` | Pickup at `30m`, alert at `2h`, repeat at `6h`; addressed ask deadlines `15m`/urgent `5m`. Ruling/blocker orphaning and RUNNING staleness use `inactive_after="72h"`. Empty `coordinator` resolves to `lead`. |
| `questions` | Human decision capture and escalation. `fallback_role="lead"`; `human_actor` defaults to the configured ANX lead actor (including adopted identity). `delivery_cli` is an optional absolute executable; Different simultaneous values are rejected. Unconfigured transcript heuristics inspect only `~/.claude`. |
| `review` | Exact-head ledger, default `family="ledger"`; the legacy family remains configurable. Result files, start IDs, timestamps and hashes must agree before SHIP qualifies. |
| `escalation` | `card` must resolve for integrated operation; `internal_to` routes toolkit health findings except repeated loop failures, which go directly to ANX Decide on the loop card. `internal_senders=[]` uses `internal_to` as the sender excluded from pickup scans; an explicit list overrides it. |
| `lane` / `agentctl` | Pool-based delegation, bounded runtime, capacity retries, durable handoff and leases. `agentctl.pools` values are selectors: `pool:<name>`, `family:<name>`, `model:<provider/model>`, or `harness:<name>` joined with `+` (for example `harness:opencode+model:morph/morph-kimik31`). Selectors pass through unchanged; a lane plan may also name one directly. Provider 429 retry evidence is reported as rate-limited, never as a hung run. No live launch is implicit in a read command. |
| `loop` | Cadence, progress budget, stop rule and per-loop overrides. At `fail_report_after=3`, failures create an ANX Decide ask on the loop card; continuing failures repeat after `failure_realert_intervals=["1h","6h","1d"]`, then at the last interval indefinitely. Intervals are positive, nondecreasing durations and may be overridden per loop. Successful cycles reset the streak and pending escalation; STOP suppresses runs. See [loop recovery and mirror replay](LOOP-RECOVERY.md). |
| `wait` | Bus/events/CI/ANX observations, settle/debounce and optional bounded backoff. Timeouts are explicit and finite. |
| `reconciler` / `limits` | Bounded stock and age checks; stale leases, missing receipts and projection drift become findings. |
| `merge` | Hold/review/check policy consumed by coordination helpers; no top-level merge command in v0.1. |
| `launchd` | macOS lifecycle labels, absolute Python and PATH. PATH must include expanded `~/.local/bin`; generated jobs do not inherit an interactive shell. Optional `observed_labels` names external, unowned periodic jobs for doctor-only checks of the target manifest, cadence, last exit status and source version; these labels are not installed, removed or template-compared. |
| `rc_host` | Optional internal deployment integration; disabled by default. |
| `shadow` | Read-only source paths and observation freshness; a 24-hour clean window is required for eligibility. |
| `deliver` | `stop_requires_waiter=true` requires a held wait lock or a waiter-refreshed cursor within `waiter_fresh_seconds=180` at Claude Stop; unread actionable rows always block within the loop budget. |
| `lanes` | Optional per-session usage metadata; `session_id` pins a Claude or OpenCode session. `harness="opencode"` reads that lane's local OpenCode SQLite store (below `config_dir`) read-only instead of Claude transcripts. Only `resume="headless"` permits Claude resume; absent/empty never resumes. A lane can override `stop_requires_waiter`; absent inherits `[deliver]`. Shared account/cwd pairs remain ambiguous. |
| `mcp` | Connector identity, archive boundaries, optional train metadata, and a manifest registry; details below. |

`mode = "live"` is the default top-level setting. `mode = "shadow"` blocks live write verbs before their external-tool calls. Shadow sweeps can write local dedupe state and relay output; MCP serving requires live mode.

### MCP settings

```toml
[mcp]
identity = "ck-sample-mcp" # default ck-<project>-mcp; never selected by callers
train_state_dir = ""      # empty disables train-file observation
train_pattern = ""        # empty disables train links in traces
ref_roots = [".coordkit/state/refs"] # default <root>/state/refs
[mcp.projects]
other = "../other/coordkit.toml"
```

Ref roots can use directory-segment globs, such as `~/project/data/run-*`. Candidates resolve before containment matching, so symlinks cannot escape the declared namespace. No arbitrary caller path is accepted. Project selection retains the entry connector's identity and authentication; writes require both entry and target write gates.

`open_questions` exposes `needs_decision` for tier 2+ questions. `[human-only]` and the legacy human guard spelling are both accepted.

## Integration and lifecycle

`init` provisions ANX resources; `doctor` reports missing dependencies, stale state, hook/job drift, dead letters and roster drift. The `shadow_guard` line reports the manifest's current mode and separately states the guard self-test result; the self-test proves that writer routes would be refused in shadow mode, not that the manifest is currently in shadow. The `roster` line warns when an enrolled agent is silent past `roster_silent_after` with no listening evidence, when an unenrolled, non-alias, otherwise undeclared name posts more than `roster_unenrolled_posts` rows in 24 hours, or when an enrolled agent receives more than `roster_sink_received` addressed rows in 24 hours and sends none. It reads a bounded bus tail and prints counts plus at most five sample names per category. `up --plan` reports changes; `up --apply` installs macOS launchd jobs and configured hooks with receipts. `down` reverses managed jobs and hook changes, refusing conflicting user edits. Standalone bus commands work on Linux; no systemd installer ships in v0.1.

Hooks have a five-second backstop and fail open. Commands generated from a source checkout retain its import path. A normal wheel/venv install emits no PYTHONPATH override; a managed `current` symlink remains lexical in generated commands. `COORDKIT_HOME` overrides the import root (or names a checkout containing `src/`).

Shadow comparison uses frozen pure queries, with semantic fingerprints excluding comments and docstrings. Functional drift fails closed; changing explanatory text does not require changing a deployed parser. Deployment provenance belongs with the [reference example](../examples/omi/CUTOVER.md).

The optional tool surfaces and their prerequisites are listed in [README](../README.md). See [ADDRESSED](ADDRESSED.md), [QUESTIONS](QUESTIONS.md), [FOOTGUNS](FOOTGUNS.md), [MCP](MCP.md) and [SECURITY](SECURITY.md) for the detailed behavior.
