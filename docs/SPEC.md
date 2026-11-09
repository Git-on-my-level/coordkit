# coordkit v0.1 contract

coordkit is a manifest-driven, agent-native local message bus. Python 3.11+, standard-library runtime, POSIX filesystem locks and process groups. Complexity belongs in optional integrations. The local bus remains the record even when an integration is unavailable.

## Storage and delivery

The bus is append-only UTF-8 JSON Lines. Rows carry `id`, `ts`, `frm`, `to`, `kind`, `topic`, and `text`; older rows without IDs remain readable. Partial trailing appends are ignored until complete. File locking serializes writers. Large bodies spill to a ref file; readers can request full text. Ref files and cursors live under the configured bus state directory.

CLI `--full` and MCP `ref_read` share structured-ref validation under `[mcp].ref_roots`.
Text such as `[full: PATH]` never authorizes a read. Full text prefers `spill_ref`,
then `ref`; a missing, unreadable or disallowed ref leaves CLI inline text visible.

Kinds include messages, asks, approvals, rulings, claims, handoffs, blockers, decisions, acknowledgements, holds, state transitions, and structured signals. Replies use `re`. A ruling names one owner; only that owner's acknowledgement closes it. Ownerless approvals/rulings publish with `owner_missing=true` and enter the hourly digest after 30 minutes unless exempted with `no_owner_required`. A later explicit owner or claim clears the topic finding. A blocker names an owner and what would unblock it; a later decision or answer closes it. Holds are evaluated in log order, scoped to a PR and the authorized poster. Unrelated PR mentions and bystander replies cannot clear another agent's obligations.

`bus wait --to NAME` uses physical row cursors and the configured wake kinds. Structured signal/state waits match their fields rather than prose. A cursor proves scanning, not human reading or task completion. No bus merge, replication, or cross-host consensus is provided.

## Manifest

`coordkit.toml` is discovered by walking up from the working directory; `--manifest PATH` overrides discovery. Paths resolve relative to the manifest, with `~` expansion. Unknown keys and wrong types fail with `ManifestError`; durations use units such as `15m` or `2h`. The authoritative field list and defaults are dataclasses in `src/coordkit/manifest.py`.

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
| `addressed` | Named agents, ordered role members, optional human roles, relay and deadlines. Only configured roles and built-in `lead` supply lead names; `relay` is special only when configured. `internal_senders=["coordkit","coordkit-report"]` suppresses normal internal pickup reminders, retaining urgent ones. |
| `obligations` | Pickup at `30m`, alert at `2h`, repeat at `6h`; addressed ask deadlines `15m`/urgent `5m`. Empty `coordinator` resolves to `lead`. |
| `questions` | Human decision capture and escalation. `fallback_role="lead"`; `human_actor` defaults to the configured ANX lead actor (including adopted identity). `delivery_cli` is an optional absolute executable; Different simultaneous values are rejected. Unconfigured transcript heuristics inspect only `~/.claude`. |
| `review` | Exact-head ledger, default `family="ledger"`; the legacy family remains configurable. Result files, start IDs, timestamps and hashes must agree before SHIP qualifies. |
| `escalation` | `card` must resolve for integrated operation; `internal_to` routes toolkit health findings. `internal_senders=[]` uses `internal_to` as the sender excluded from pickup scans; an explicit list overrides it. |
| `lane` / `agentctl` | Pool-based delegation, bounded runtime, capacity retries, durable handoff and leases. No live launch is implicit in a read command. |
| `loop` | Cadence, progress budget, stop rule and per-loop overrides; repeated failures escalate once until recovery. |
| `wait` | Bus/events/CI/ANX observations, settle/debounce and optional bounded backoff. Timeouts are explicit and finite. |
| `reconciler` / `limits` | Bounded stock and age checks; stale leases, missing receipts and projection drift become findings. |
| `merge` | Hold/review/check policy consumed by coordination helpers; no top-level merge command in v0.1. |
| `launchd` | macOS lifecycle labels, absolute Python and PATH. PATH must include expanded `~/.local/bin`; generated jobs do not inherit an interactive shell. |
| `rc_host` | Optional internal deployment integration; disabled by default. |
| `shadow` | Read-only source paths and observation freshness; a 24-hour clean window is required for eligibility. |
| `deliver` | `stop_requires_waiter=true` requires a held wait lock or a waiter-refreshed cursor within `waiter_fresh_seconds=180` at Claude Stop; unread actionable rows always block within the loop budget. |
| `lanes` | Optional per-session usage metadata; `session_id` pins a Claude session. Only `resume="headless"` permits Claude resume; absent/empty never resumes. A lane can override `stop_requires_waiter`; absent inherits `[deliver]`. Shared account/cwd pairs remain ambiguous. |
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

`init` provisions ANX resources; `doctor` reports missing dependencies, stale state, hook/job drift and dead letters. `up --plan` reports changes; `up --apply` installs macOS launchd jobs and configured hooks with receipts. `down` reverses managed jobs and hook changes, refusing conflicting user edits. Standalone bus commands work on Linux; no systemd installer ships in v0.1.

Hooks have a five-second backstop and fail open. Commands generated from a source checkout retain its import path. A normal wheel/venv install emits no PYTHONPATH override; a managed `current` symlink remains lexical in generated commands. `COORDKIT_HOME` overrides the import root (or names a checkout containing `src/`).

Shadow comparison uses frozen pure queries, with semantic fingerprints excluding comments and docstrings. Functional drift fails closed; changing explanatory text does not require changing a deployed parser. Deployment provenance belongs with the [reference example](../examples/omi/CUTOVER.md).

The optional tool surfaces and their prerequisites are listed in [README](../README.md). See [ADDRESSED](ADDRESSED.md), [QUESTIONS](QUESTIONS.md), [FOOTGUNS](FOOTGUNS.md), [MCP](MCP.md) and [SECURITY](SECURITY.md) for the detailed behavior.
