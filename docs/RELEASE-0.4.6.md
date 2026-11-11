# coordkit 0.4.6

Runtime source is pinned to main commit `55054b86ac23df85750877a145076433714c4d8e`.
The release commit changes only version metadata, the wheel example and these notes.

- #25: Hermes harness adapter. A lane can live in a Hermes Agent profile
  (`[[lanes]] harness = "hermes"`, `profile`, optional `turn_timeout`), with
  its own model, memory and skills. See [Hermes lanes](HERMES.md).
  - A Hermes lane has no process between turns. A row addressed to the lane,
    to a role it belongs to, or naming it as owner starts a detached runner
    that runs one `hermes -z` turn in the lane's session, then another round
    (three at most) if rows arrived meanwhile.
  - Such a lane reads as listening while a turn can be started, and as not
    listening, with the reason, after a failed wake or turn.
  - The wake debounce ends when the woken turn finishes cleanly.
  - A turn gets a fixed environment (not the sender's) with `HERMES_HOME` set
    to the lane's profile home.
  - Per-turn receipts feed the usage view and `doctor` `model_drift`.
  - `coordkit hooks install|status|uninstall --hermes-lane NAME` manages an
    optional turn-start delivery hook in the profile's `config.yaml`.
  - `doctor` shows `hermes` in place of the report-only `hermes-acp` line.

New manifest keys, all optional and only for Hermes lanes: `lanes.harness =
"hermes"`, `lanes.profile`, `lanes.turn_timeout`. A manifest without a Hermes
lane behaves as in 0.4.5.

Known limits: rows that arrive mid-turn are handled by the next round, not
inside the running turn; usage counts only coordkit-started turns.

Install with `sh scripts/install.sh v0.4.6`; roll back by repointing the current
release to v0.4.5 or with `coordkit update --rollback`.
