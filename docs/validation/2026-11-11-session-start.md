# Unmanaged Claude SessionStart source validation

This is source preparation for draft PR #23, based on
`38b717dd36cfff9eb65fb3cf820787a633cad769`. It does not establish live lane
coverage, actual agentctl child admission/collection, official installed-agentctl
acceptance or release readiness. No existing lane lifecycle was changed.

## Supported native mechanism

The official [CLI reference](https://code.claude.com/docs/en/cli-reference) and
[Setup reference](https://code.claude.com/docs/en/hooks#setup) document
`claude --init-only` as running Setup/SessionStart without starting a conversation.
The flag is absent from this installed binary's help, but the native probe
accepted it with no prompt. The
[hook reference](https://code.claude.com/docs/en/hooks#persist-environment-variables)
documents the SessionStart session ID and private env-file persistence for later
Bash commands.

Exact binary: **Claude Code 2.1.322**.
SHA-256: `a9d237949db816a0b7afd88a9a8e1df36b3a597a30f5a41d1c0539eb180a3450`.
Hook interpreter: Python 3.11.15. Measurement time and all sanitized booleans are
in the [native probe record](2026-11-11-session-start.json).

The [reproducible opt-in probe](../../scripts/probe-session-start.py) creates only
throwaway homes, config directories, working directories and temp storage. It
clears the inherited environment, uses a noncredential dummy API key, supplies
only fixture hooks with empty plugin/MCP configuration, and denies network in a
macOS sandbox. A local socket challenge verifies the network denial before native
execution. Real home/config, keychain and system managed-settings reads are
denied, apart from the native binary and Python runtime paths needed to execute.
Real-home writes are denied. Each native process has a 30-second deadline and
its exact process group is terminated/reaped on every completion path.

Two independent cases each first capture a native-generated session. That
observed ID becomes an explicit fixture pin in a fresh isolated context; the
next native SessionStart confirms the same ID before the bridge is checked.
No session ID is inferred from cwd or invented by the probe. A private wrapper
captures the bridge command's own native stdin/env handle, then invokes its exact
generated argv with that unchanged stdin. An unrelated fixture hook stays present.
The installed native binary supplies a separate env handle to each hook, so the
unrelated hook's handle cannot stand in for the bridge's target.

Both cases prove native SessionStart delivery, a matching privately created 0600
export file, distinct sessions, and preservation of the unrelated hook. Clean
Bash manually sources each private env file and invokes a fake agentctl; only
provider/session equality booleans leave that subprocess. A second fake consumer
proves a managed execution marker prevents the persisted block from exporting
an explicit pair. No real agentctl command, provider prompt or model turn is run.
Temporary native handles and contexts are removed; no raw session/env path is
retained in the public record.

Manual sourcing verifies generated shell binding. It **does not** prove an actual
model-driven Bash tool consumed it. These probes are distinct from the parent's
pending actual-child full-Stop acceptance tests.

Example reproduction from a checkout on macOS:

```sh
PYTHONPATH=src python3.11 scripts/probe-session-start.py \
  --claude /path/to/claude --scratch-dir /path/to/scratch \
  --output /path/to/sanitized-results.json
```

## Source behavior and focused checks

The owned synchronous SessionStart command shares the existing delivery receipt,
reconciliation and uninstall lifecycle. It accepts only a matching configured
pin or existing saved binding to a known Claude lane; ambiguous scope skips.
It ignores cwd, ambient coordination names and transcript fallback and creates
no session registry. The existing global/per-lane collection switch applies.

Managed execution/adapter/authority markers, including empty values, skip new
exports. The persisted Bash block checks them again when sourced. This preserves
agentctl's automatic managed parent linkage instead of taking its explicit
declaration branch. Ambiguous inherited managed context remains a coverage limit.

The bridge enforces agentctl's bounded UTF-8/no-control session contract and
shell-quotes the value as data. Descriptor-based traversal rejects symlinks,
untrusted directories, nonprivate/unowned/nonregular/multiply-linked targets and
unknown layouts. The observed layout is `config/session-env/SESSION/FILE`;
future unrecognized layouts fail open. The native handle initially names a
missing leaf; secure exclusive creation uses mode 0600 within checked existing
parents. Missing handles/parents skip. Existing content is preserved with an
idempotent bounded append and a nonblocking lock. Limits are 32 KiB input,
256 KiB saved bindings, 64 KiB env content, 2 KiB appended data and a 250 ms core
deadline inside the five-second bootstrap deadline. Failures return success
without output or a decision.

```sh
PYTHONPATH=src python3.11 -m unittest \
  tests.test_session_start tests.test_deliver tests.test_stop_hook \
  tests.test_hook_concurrency tests.test_install_paths tests.test_state_registry
python3.11 -m compileall -q src tests scripts/probe-session-start.py
bash scripts/leak-check.sh
git diff --check
```

**98 focused tests passed** in 1.055 seconds on Python 3.11.15. Coverage includes
pins/bindings and conflicts, two-session isolation, managed/ambiguous markers,
managed markers at later Bash sourcing, kill switches, UTF-8/control/injection
input, unsafe/missing targets, short writes, content/append bounds, idempotence,
locks/deadlines, bootstrap failures, plugin preservation, old receipt migration,
reconcile conflicts and owned uninstall. Existing Stop tests retain authoritative
attention/waiter behavior, old retained children, timeout/unsupported-binary
fail-open and the shared two-block/600-second budget. External tools/destinations
are stubbed; any bus fixtures are isolated synthetic files.

Compile, leak and whitespace checks pass. The full required suite and hosted CI
were deliberately deferred to the parent, as directed, while agentctl work is
active. No full-suite claim is made for this prepared source.

## Remaining gates and operating limits

Existing unmanaged lanes adopt only through a later supported SessionStart
lifecycle. Installing settings alone does not bind their current calls; no live
session was restarted/resumed here. Unattributed legacy admissions are not
backfilled. Unknown scope, inherited managed ambiguity, unsafe env targets or
changed native layouts remain fail-open.

The official agentctl release containing #147/#148 is still pending. The parent
must later pin the exact installed dependency, refresh main/tags and select the
next free coordkit version, run the full required source/hosted gates, perform
20 read-only real-journal measurements and actual-child full-Stop collection,
error, budget, timeout and kill-switch probes, then obtain independent review of
the exact final head. No version bump, push, mark-ready, merge, tag, release,
installation or live configuration change belongs to this preparation.
