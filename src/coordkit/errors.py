"""Dependency-free public error types, also used by fail-open hook adapters."""
class ManifestError(ValueError):
    exit_code = 2
