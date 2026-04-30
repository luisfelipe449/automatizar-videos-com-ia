"""Per-job in-process flags for visual-provider availability.

In-process state is sufficient because idempotent retries re-check each time —
a fresh Python run simply rediscovers any quota error on its next call.
"""

from __future__ import annotations

_IMAGEN = "imagen"
_POLLINATIONS = "pollinations"

# Keyed by job_id → set of provider names that are known to be unavailable.
_unavailable: dict[str, set[str]] = {}


def _mark(job_id: str, provider: str) -> None:
    _unavailable.setdefault(job_id, set()).add(provider)


def _has(job_id: str, provider: str) -> bool:
    return provider in _unavailable.get(job_id, ())


def mark_imagen_unavailable(job_id: str) -> None:
    _mark(job_id, _IMAGEN)


def is_imagen_unavailable(job_id: str) -> bool:
    return _has(job_id, _IMAGEN)


def mark_pollinations_unavailable(job_id: str) -> None:
    _mark(job_id, _POLLINATIONS)


def is_pollinations_unavailable(job_id: str) -> bool:
    return _has(job_id, _POLLINATIONS)


def reset(job_id: str) -> None:
    """Clear all flags for `job_id`. Primarily used by tests."""
    _unavailable.pop(job_id, None)
