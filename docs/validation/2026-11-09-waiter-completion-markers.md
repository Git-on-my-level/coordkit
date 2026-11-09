# Waiter completion marker validation

Captured: 2026-11-09T14:21:41Z. Platform: macOS, Python 3.12.13.

A complete header can survive a crash during multiline output, so a header is
insufficient evidence for receiver-log deduplication. The regression reproduced
lost delivery on the previous implementation at four cuts: after the header,
mid-body, before the completion marker, and before its final newline.

Both normal and `--full` waiter deliveries now buffer the formatted body and a
final `-- end <id>` line in one write. Recovery recognizes only complete,
newline-terminated marker lines in the last 1 MiB of the receiver log. An
exact marker-shaped line in the formatted body gains one leading space before
writing, so neither a torn body nor another message's text can forge completion.
The collision regression interrupts the actual buffered write after body lines
naming both the current ID and another ID, then checks complete replay and no
duplicate on retry. An unmarked delivery is replayed, adding a separating newline
after an unterminated fragment. Legacy unmarked single-line deliveries also replay once. Receiver
flush and fsync still precede cursor commit, including when recovering an
already completed delivery. Marker lifecycle is recorded in the state registry.

Validation:

- Full `PYTHONPATH=src python3.12 -m unittest discover -v`, with a temporary
  `HOME` and `COORDKIT_COLD_CLI_BUDGET=1.0`: **871 tests passed in 313.965 seconds**.
- Torn multiline regression verifies the full replay, cursor advancement, and
  no duplicate after another restart with the cursor reset. Normal-output
  coverage verifies one-time legacy replay and completion-marker deduplication.
- Existing coverage retains bounded-tail boundaries, exact IDs, append position,
  receiver sync failures, and a real crash between fsync and cursor commit.
- Compile, leak and whitespace checks passed.
- The continuation-fixture ResourceWarning appeared; its named child had exited
  when checked after the full run.

This supersedes the header-based recovery rule in the earlier
[receiver deduplication validation](2026-11-09-waiter-receiver-dedup.md).
Persistent recovery requires appending to the same readable regular file with
the completion marker still inside the bounded tail. Pipes and terminals have
no persistent receiver-log deduplication. The Linux reopening path was not
exercised on this macOS host.
