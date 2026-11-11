# Hermes lanes

A Hermes lane is one named session in one Hermes Agent profile. It has no process between turns: coordkit starts a turn when addressed
work arrives, and the lane's memory, skills and model live in the profile.

```toml
[addressed.agents.ops]
runtime = "hermes"
fallback = "coordinator"

[[lanes]]
name = "ops"
harness = "hermes"
profile = "ops"                          # hermes -p ops
config_dir = "~/.hermes/profiles/ops"    # the profile home
cwd = "~/project"
model = "provider-model-id"              # passed as -m; "default" uses the profile's own model
effort = "default"                       # anything else is passed as --reasoning
turn_timeout = "2h"                      # optional, 60s..24h
```

One lane per profile. A Hermes lane takes no `session_id`, `resume` or `pane`.
Create and configure the profile with Hermes itself (`hermes profile create`,
`hermes -p NAME config set model.provider ...`); coordkit never writes
credentials or model settings.

## What happens on a wake

An addressed row for the lane starts a detached runner
(`python -m coordkit.adapters.hermes_turn`). The runner:

1. Takes the lane's turn lock. A second runner exits at once; the first one will
   see the new rows.
2. Reads the rows after the lane's delivery cursor, puts up to 20 of them in the
   prompt, and advances the cursor.
3. Runs one gateway-free turn:
   `hermes -p PROFILE --in CWD --no-restore-cwd --accept-hooks [--resume SESSION] --output-format json --usage-file F [-m MODEL] -z PROMPT`,
   with `COORDKIT_AS=LANE` in its environment. The first turn starts a new
   session, which the runner titles with the lane name; later turns resume the
   session id the last turn reported. A session that no longer resumes is
   replaced once.
4. Appends a receipt (result, session, model, token counts).
5. Looks at the cursor again. If addressed rows arrived during the turn it runs
   another round, at most three per wake. This is the lane's stop guard: a
   Hermes turn cannot be held open the way a Claude Stop hook holds one.

A failed or timed-out turn ends the wake. The row stays in the lane's bus inbox.

## Listening

`bus state`, the roster and the deaf sweep treat a Hermes lane as reachable
while a turn can be started: a turn is running, or the last wake and turn did
not fail. After a failed wake or turn the lane reads as not listening, with the
reason, until a later wake succeeds, so its fallback hears about it.

The wake debounce ends as soon as the woken turn finishes cleanly. A row posted
a few seconds after a turn ended starts a new turn instead of waiting out the
window. After a failure the debounce still holds, so a broken provider is not
retried on every row.

## Turn-start hook (optional)

```sh
coordkit hooks install --hermes-lane ops
coordkit hooks status --hermes-lane ops
coordkit hooks uninstall --hermes-lane ops
```

This appends one marked `hooks.pre_llm_call` entry to the profile's
`config.yaml`. Hermes runs it once at the start of a turn and adds the lane's
unread addressed rows to the user message. Coordkit-started turns already carry
their rows in the prompt, so the hook matters for turns you start in the profile
yourself. It shares the delivery cursor with the runner; a row is shown once.

Coordkit has no YAML parser. If the profile already has a `hooks:` map, install
refuses and prints the entry to add by hand. The runner passes `--accept-hooks`,
so every hook in that profile's config runs without a consent prompt: keep the
profile's config under your own control.

## Usage and model drift

Usage comes from the receipts, one per turn, under
`$XDG_STATE_HOME/coordkit/hermes/` (default `~/.local/state`). `turns` counts
provider calls. Context size is not reported by a one-shot run and stays zero.
Turns started outside coordkit have no receipt and are not counted.
`doctor` reports `model_drift` when a turn ran on a model other than the lane's.

## Limits

- Rows that arrive mid-turn are handled by the next round, not inside the
  running turn. Hermes has no per-tool-call context hook.
- Three rounds per wake. Later rows wait for the next addressed row or the
  obligation pickup sweep.
- The runner does not read the Hermes session store. A turn you run by hand in
  the same session is serialized by Hermes's own turn lease.
