"""Preconfigured tenacity retry decorators + a predicate for transient HTTP errors.

`default_retry` is the generic wrapper for LLM / API calls. `network_retry` is
tuned for flaky network conditions and matches only transient errors, so real
bugs still surface immediately.
"""

from __future__ import annotations

import httpx
import requests
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

# Exception classes that are ALWAYS safe to retry (transport-level failures).
_RETRYABLE_EXC_TYPES: tuple[type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
)

# HTTP-level exception types whose `.response.status_code` we inspect to decide
# whether a retry is warranted (only 5xx / 429 should be retried).
_HTTP_STATUS_EXC_TYPES: tuple[type[BaseException], ...] = (
    requests.exceptions.HTTPError,
    httpx.HTTPStatusError,
)

# Substrings (lowercased) that indicate a transient HTTP-level problem
# even when the exception type isn't one of the well-known ones above.
_RETRYABLE_SUBSTRINGS: tuple[str, ...] = (
    "500",
    "502",
    "503",
    "504",
    "429",
    "timeout",
    "rate_limit",
    "rate limit",
)


def is_retryable_http(exc: BaseException) -> bool:
    """Return True when `exc` looks like a transient network/HTTP error."""
    if isinstance(exc, _RETRYABLE_EXC_TYPES):
        return True
    if isinstance(exc, _HTTP_STATUS_EXC_TYPES):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status >= 500 or status == 429
        # Fall through to substring match when the response/status is missing.
    message = str(exc).lower()
    return any(token in message for token in _RETRYABLE_SUBSTRINGS)


def network_retry_predicate(exc: BaseException) -> bool:
    """Public alias so downstream code (e.g. YouTube resumable loop) can reuse it."""
    return is_retryable_http(exc)


# Generic retry: 3 attempts, exponential 1 → 8s with ±1s jitter.
# Uses the same transient-error predicate as `network_retry` so deterministic
# failures (e.g. Groq 400, ScriptGenerationError) fail fast instead of burning
# all 3 attempts.
default_retry = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=1, max=8, jitter=1),
    retry=retry_if_exception(is_retryable_http),
    reraise=True,
)

# Network-specific: 5 attempts, longer backoff, only on transient errors.
network_retry = retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential_jitter(initial=1, max=30, jitter=2),
    retry=retry_if_exception(is_retryable_http),
    reraise=True,
)
