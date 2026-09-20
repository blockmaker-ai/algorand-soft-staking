"""Exact accounting primitives; no signing or database access."""
from datetime import datetime, timezone
import hashlib
import json

MAX_UINT64 = 2**64 - 1


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("A UTC offset is required")
    return result.astimezone(timezone.utc)


def micros(delta):
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def integer(value):
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("Base-unit amounts must be integers or decimal strings")
    result = int(value)
    if str(result) != str(value) or not 0 <= result <= MAX_UINT64:
        raise ValueError("Invalid base-unit amount")
    return result


def month_bounds(at):
    beginning = at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    following = beginning.replace(year=at.year + 1, month=1) if at.month == 12 else beginning.replace(month=at.month + 1)
    return beginning, following


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
