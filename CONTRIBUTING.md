# Contributing

Keep the core small and runtime dependencies in the Python standard library. Put harness, transport and service-manager complexity in optional integrations. Support Python 3.11 and 3.12 on POSIX systems; automated lifecycle installation currently targets macOS launchd.

Before submitting a focused change:

```sh
python3 -m compileall -q src tests
PYTHONPATH=src python3 -m unittest discover
bash scripts/leak-check.sh
```

Use `unittest`, the existing fake external CLIs, and synthetic fixtures. Preserve protocol edge cases and test counts when replacing fixtures. Do not copy production traffic, credentials, private paths or incident receipts into the repository. Intentional compatibility examples require exact line hashes in `scripts/leak-allowlist.txt`; only `examples/omi/` can be allow-listed.

Tests should fit a two-minute local budget; CI has a five-minute hard limit per job. The cold CLI test is sensitive to host load: record a failure and rerun that test on its own before attributing it to a code change. The default cold CLI budget is 0.3 seconds; set `COORDKIT_COLD_CLI_BUDGET` explicitly for slower environments. CI uses 1.0 second and still executes the assertion.

Explain the violated contract in regression tests. Keep comments about mechanisms and failure modes, without personal attributions or private incident IDs. New persisted markers need a lifecycle entry in `tests/state_registry.py`. Preserve receipt-first writes, bounded external calls and fail-open harness hooks.

Open a focused PR describing behavior, validation and remaining platform limits. Security reports should follow [SECURITY](docs/SECURITY.md). Contributions are licensed under Apache-2.0.
