"""Quota detection and per-job in-process flags for visual-provider availability.

In-process state is sufficient because idempotent retries re-check each time —
a fresh Python run simply rediscovers any quota error on its next call.
"""

from __future__ import annotations

_IMAGEN = "imagen"
_POLLINATIONS = "pollinations"
_VIDEO = "video"



def is_daily_quota_error(exc: BaseException) -> bool:
    """True when `exc` reports a Gemini allowance spent until the daily reset.

    Gemini's per-minute throttling and its daily cap arrive alike: 429
    RESOURCE_EXHAUSTED, "You exceeded your current quota", a `quotaMetric`.
    Only the violated `quotaId` names the period
    (`GenerateRequestsPerDayPerProjectPerModel-FreeTier` vs `...PerMinute...`).
    A per-minute 429 clears within a minute and must stay retryable.
    """
    return "perday" in str(exc).lower()


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


def mark_video_unavailable(job_id: str) -> None:
    _mark(job_id, _VIDEO)


def is_video_unavailable(job_id: str) -> bool:
    return _has(job_id, _VIDEO)


def reset(job_id: str) -> None:
    """Clear all flags for `job_id`. Primarily used by tests."""
    _unavailable.pop(job_id, None)
