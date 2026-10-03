"""Small shared helpers: UTC time formatting, canonical JSON, ids."""

from __future__ import annotations

import datetime as _dt
import json
import secrets

UTC = _dt.timezone.utc
ISO_UTC = "%Y-%m-%dT%H:%M:%SZ"


def now_utc() -> _dt.datetime:
    """Current time as an aware UTC datetime."""
    return _dt.datetime.now(UTC)


def iso(dt: _dt.datetime) -> str:
    """Format an aware datetime as ``2026-10-20T09:15:00Z``."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime(ISO_UTC)


def parse_iso(text: str) -> _dt.datetime:
    """Parse an ISO-8601 timestamp, accepting ``Z`` and space separators."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty timestamp")
    s = text.strip().replace(" ", "T", 1) if "T" not in text else text.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = _dt.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def epoch(dt: _dt.datetime) -> float:
    """POSIX seconds for an aware datetime."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def minus(dt: _dt.datetime, **kw) -> _dt.datetime:
    """``minus(now, minutes=5)`` helper."""
    return dt - _dt.timedelta(**kw)


def plus(dt: _dt.datetime, **kw) -> _dt.datetime:
    """``plus(now, minutes=90)`` helper."""
    return dt + _dt.timedelta(**kw)


def canonical(obj) -> str:
    """Canonical JSON used for hashing: sorted keys, no whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def new_secret() -> str:
    """Per-session rotating-code secret (hex, 32 bytes)."""
    return secrets.token_hex(32)


def new_ref(prefix: str = "P") -> str:
    """Random participant reference, e.g. ``P-4F2A``."""
    return "%s-%s" % (prefix, secrets.token_hex(2).upper())


def clamp(value, low, high):
    return max(low, min(high, value))
