# coordkit 0.4.7

Runtime source is pinned to main commit `626eb2ebf09831d9d15ddd20197d8bb93caf926a`.
The release commit changes only version metadata, the wheel example and these notes.

- #26: scheduled wakes. `coordkit wake-at LANE "text" --in 2h` (or `--at` an
  ISO time with a zone) posts one row to a lane at a set time; `--list` and
  `--cancel ID` manage pending timers. See
  [scheduled wakes](V02.md#scheduled-wakes).
  - A timer is a file under `state/wakes/timers/`. The periodic `wake-scan`
    appends its row when due (a `handoff` from `coordkit`, id `wake-at-ID`)
    and the ordinary wake path delivers it.
  - Delivery is at least once; a failed append is retried five minutes later
    and set aside as `ID.failed` after twelve attempts.
  - Only a live manifest fires timers.
  - It exists for lanes that cannot hold a timer of their own, such as Hermes
    lanes (0.4.6).

No manifest keys change. Timers fire only where `wake-scan` runs on a schedule.
Install with `coordkit update --apply`; roll back with `coordkit update --rollback`.
