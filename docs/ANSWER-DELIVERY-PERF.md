# Answer sweep performance probe

Run from the repository root with `PYTHONPATH=src:. python3 scripts/benchmark-answers.py`.
It uses synthetic test state and a mocked ANX transport, with no live services.
The fixture contains 40 unchanged response-less unread answers, their legacy
bus rows, and 1,500 retained read receipts. One warmup creates compatibility
receipts; three further sweeps run under `cProfile`, counting `read_json` calls
and measuring elapsed time with `perf_counter`. The temporary fixture is removed
on exit. To compare revisions, run the same script against each revision's `src`
and test fixture; this script itself is new in the fix.

Measured on 2026-11-10, using the same interpreter and host for both revisions:

| Implementation | Sweep times (seconds, profiled) | Median | JSON reads per sweep |
| --- | --- | --- | --- |
| Before, `72c48672b725aa140b737abb6c37bc9da4edfb91` | 4.3149, 4.5273, 4.2663 | 4.3149 | 66,721 |
| Shared snapshot/index fix | 0.1532, 0.1401, 0.1497 | 0.1497 | 1,621 |

The median improves by 28.8 times; JSON reads fall by 97.6%. Absolute timings
depend on filesystem and host load. The regression tests enforce the structural
bound instead: one bus snapshot before work and a refresh after each publication
attempt, one pending-receipt index per sweep, and one direct receipt lookup per
unchanged terminal unread item. Retained receipts still require one linear scan
per sweep; they no longer multiply the work for each unread item.
