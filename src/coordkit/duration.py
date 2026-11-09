"""Shared scalar duration parsing without orchestration schema imports."""
import re
from .errors import ManifestError


def duration(value: str) -> int:
    match = re.fullmatch(r"(\d+)([smhd])", value) if isinstance(value, str) else None
    if not match or int(match[1]) == 0:
        raise ManifestError(f"invalid_duration: {value!r}")
    return int(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]
