"""YouTube Data API v3 resumable upload (Phase 8 of the pipeline).

Single responsibility: take a rendered MP4 + a `Script`, authenticate via a
pre-obtained OAuth2 refresh token, upload the file with a resumable
`videos.insert` call, and persist the returned `videoId`. Never re-runs the
OAuth consent flow at runtime — the refresh token is expected to have been
generated once out-of-band by `scripts/bootstrap_youtube_oauth.py`.
"""

from __future__ import annotations

import datetime
import json
import socket
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from tenacity import (
    Retrying,
    RetryError,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app import config
from app.config import require
from app.models import Script
from app.utils import filepaths

_LOG_PREFIX = "[youtube_service]"

_TOKEN_URI = "https://oauth2.googleapis.com/token"
_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
_CATEGORY_PEOPLE_AND_BLOGS = "22"
_ALLOWED_PRIVACY = ("private", "unlisted", "public")

# Transient HTTP statuses worth retrying. 4xx (except 429) and permanent 5xx
# errors surface immediately — they're not going to fix themselves.
_RETRYABLE_STATUSES = {500, 502, 503, 504}
_RETRYABLE_EXC_TYPES: tuple[type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    socket.timeout,
)
_MAX_UPLOAD_ATTEMPTS = 5


class YouTubeUploadError(RuntimeError):
    """Raised when the upload cannot complete (auth, validation, or API error)."""


# --------------------------------------------------------------------------- #
# Helpers (pure where possible)
# --------------------------------------------------------------------------- #

def _ensure_shorts_hashtag(description: str) -> str:
    """Defensive: ensure the description contains `#Shorts` (Script already does).

    Case-insensitive check so we don't double-append when the user wrote
    `#SHORTS` or `#shorts` by hand.
    """
    return description if "#shorts" in description.lower() else f"{description} #Shorts"


def _resolve_privacy(override: str | None) -> str:
    """Pick `override` when set, else fall back to the configured default."""
    chosen = (override or config.YOUTUBE_PRIVACY_STATUS or "private").strip().lower()
    if chosen not in _ALLOWED_PRIVACY:
        raise YouTubeUploadError(
            f"invalid privacy_status {chosen!r}; expected one of {_ALLOWED_PRIVACY}"
        )
    return chosen


def _validate_inputs(video_path: Path, privacy_status: str | None) -> str:
    """Fail loudly BEFORE touching the network. Returns the resolved privacy."""
    privacy = _resolve_privacy(privacy_status)

    if not video_path.exists():
        raise YouTubeUploadError(f"video not found: {video_path}")
    try:
        size = video_path.stat().st_size
    except OSError as exc:
        raise YouTubeUploadError(f"cannot stat video {video_path}: {exc}") from exc
    if size == 0:
        raise YouTubeUploadError(f"video is 0 bytes: {video_path}")

    return privacy


def _build_client():
    """Construct an authenticated YouTube Data API client from env credentials.

    The Google SDK refreshes the access token lazily on the first API call, so
    we don't call `creds.refresh(...)` ourselves. `cache_discovery=False` silences
    a noisy file-lock warning on Windows.
    """
    creds = Credentials(
        token=None,
        refresh_token=require("YOUTUBE_REFRESH_TOKEN"),
        token_uri=_TOKEN_URI,
        client_id=require("YOUTUBE_CLIENT_ID"),
        client_secret=require("YOUTUBE_CLIENT_SECRET"),
        scopes=_SCOPES,
    )
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _http_status(exc: HttpError) -> int | None:
    """Dig the numeric status out of an HttpError across client-lib versions."""
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    if status is None:
        return None
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def _is_retryable(exc: BaseException) -> bool:
    """True for transient network/HTTP errors worth backing off on."""
    if isinstance(exc, HttpError):
        status = _http_status(exc)
        return status in _RETRYABLE_STATUSES
    return isinstance(exc, _RETRYABLE_EXC_TYPES)


def _next_chunk_with_retry(request):
    """Wrap `request.next_chunk()` in tenacity, retrying only transient errors.

    Non-transient `HttpError`s (4xx, or any status outside the retry set) are
    re-raised as `YouTubeUploadError` with status + reason context so the
    caller gets a useful message on the first try.
    """
    retrying = Retrying(
        stop=stop_after_attempt(_MAX_UPLOAD_ATTEMPTS),
        wait=wait_exponential_jitter(initial=1, max=30, jitter=2),
        retry=retry_if_exception(_is_retryable),
        reraise=True,
    )
    try:
        for attempt in retrying:
            with attempt:
                return request.next_chunk()
    except HttpError as exc:
        # Either non-retryable from the start, or retries exhausted.
        status = _http_status(exc)
        reason = getattr(getattr(exc, "resp", None), "reason", "") or ""
        raise YouTubeUploadError(
            f"YouTube API error (status={status}, reason={reason!r}): {exc}"
        ) from exc
    except RetryError as exc:  # pragma: no cover — reraise=True should bypass this
        raise YouTubeUploadError(f"upload retries exhausted: {exc}") from exc
    # Should never reach here — the loop either returns or raises.
    raise YouTubeUploadError("next_chunk retry loop exited without a result")


def _write_upload_record(script: Script, video_id: str, privacy: str) -> Path:
    """Persist `{video_id, uploaded_at, privacy_status, title}` as JSON."""
    record = {
        "video_id": video_id,
        "uploaded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "privacy_status": privacy,
        "title": script.title,
    }
    path = filepaths.upload_record_path(script.job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def upload_short(
    video_path: Path,
    script: Script,
    *,
    privacy_status: str | None = None,
) -> str:
    """Upload the rendered MP4 as a YouTube Short.

    Returns the uploaded `videoId`. Writes a JSON record to
    `filepaths.upload_record_path(script.job_id)`. Raises `YouTubeUploadError`
    on any validation or API failure. `privacy_status` (if provided) overrides
    `config.YOUTUBE_PRIVACY_STATUS`.
    """
    video_path = Path(video_path)
    privacy = _validate_inputs(video_path, privacy_status)
    filepaths.ensure_job_dirs(script.job_id)

    youtube = _build_client()

    body = {
        "snippet": {
            "title": script.title,
            "description": _ensure_shorts_hashtag(script.description),
            "tags": list(script.tags),
            "categoryId": _CATEGORY_PEOPLE_AND_BLOGS,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(
        str(video_path),
        chunksize=-1,  # single-shot resumable upload — no chunk loop overhead.
        resumable=True,
        mimetype="video/mp4",
    )

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    print(
        f"{_LOG_PREFIX} uploading {video_path.name} "
        f"({video_path.stat().st_size} bytes) as {privacy}"
    )

    response = None
    while response is None:
        status, response = _next_chunk_with_retry(request)
        if status is not None:
            print(f"{_LOG_PREFIX} upload progress: {int(status.progress() * 100)}%")

    if not isinstance(response, dict) or "id" not in response:
        raise YouTubeUploadError(
            f"YouTube response missing 'id' key: {response!r}"
        )
    video_id = response["id"]

    _write_upload_record(script, video_id, privacy)
    print(f"{_LOG_PREFIX} uploaded videoId={video_id}")
    return video_id


def verify_credentials() -> dict:
    """Cheap sanity check: call `channels.list(mine=True)`. No upload.

    Returns `{"channel_id": ..., "title": ...}` for the authenticated channel.
    Raises `YouTubeUploadError` if credentials are misconfigured or the API
    call fails. Used by `scripts/bootstrap_youtube_oauth.py` and tests.
    """
    try:
        youtube = _build_client()
        response = youtube.channels().list(part="id,snippet", mine=True).execute()
    except HttpError as exc:
        status = _http_status(exc)
        raise YouTubeUploadError(
            f"verify_credentials failed (status={status}): {exc}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 — surface auth/transport errors uniformly
        raise YouTubeUploadError(f"verify_credentials failed: {exc}") from exc

    items = response.get("items") if isinstance(response, dict) else None
    if not items:
        raise YouTubeUploadError(
            "verify_credentials: channels.list returned no items"
        )
    item = items[0]
    return {
        "channel_id": item.get("id", ""),
        "title": item.get("snippet", {}).get("title", ""),
    }
