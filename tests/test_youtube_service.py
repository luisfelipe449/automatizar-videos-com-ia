"""Tests for `app.services.youtube_service` — no real YouTube API is contacted.

Strategy: monkeypatch `googleapiclient.discovery.build` to return a hand-rolled
fake client whose `videos().insert()` produces a request object with a scripted
`next_chunk()` queue. Every response / exception is stamped in via a list.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httplib2
import pytest
from googleapiclient.errors import HttpError

from app.models import Scene, Script


# --------------------------------------------------------------------------- #
# Fake Google SDK shapes
# --------------------------------------------------------------------------- #

class _FakeStatus:
    """Mimics googleapiclient.http.MediaUploadProgress."""

    def __init__(self, fraction: float):
        self._fraction = fraction

    def progress(self) -> float:
        return self._fraction


class _FakeRequest:
    """`videos().insert(...)` return value. `next_chunk()` drains a scripted queue."""

    def __init__(self, queue: list[Any]):
        self._queue = list(queue)
        self.calls = 0

    def next_chunk(self):
        self.calls += 1
        if not self._queue:
            raise AssertionError("next_chunk queue exhausted unexpectedly")
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item  # tuple (status_or_None, response_or_None)


class _FakeVideos:
    """Captures the body/part/media_body passed to `videos().insert`."""

    def __init__(self, request: _FakeRequest):
        self._request = request
        self.insert_kwargs: dict | None = None

    def insert(self, **kwargs):
        self.insert_kwargs = kwargs
        return self._request


class _FakeChannelsListRequest:
    def __init__(self, payload: dict):
        self._payload = payload

    def execute(self):
        return self._payload


class _FakeChannels:
    def __init__(self, payload: dict):
        self._payload = payload
        self.list_kwargs: dict | None = None

    def list(self, **kwargs):
        self.list_kwargs = kwargs
        return _FakeChannelsListRequest(self._payload)


class _FakeYoutube:
    def __init__(self, videos: _FakeVideos, channels: _FakeChannels | None = None):
        self._videos = videos
        self._channels = channels

    def videos(self):
        return self._videos

    def channels(self):
        return self._channels


def _make_http_error(status: int, reason: str = "Err") -> HttpError:
    """Build a real `HttpError` with a realistic `.resp.status`."""
    resp = httplib2.Response({"status": status, "reason": reason})
    # httplib2.Response preserves `reason` as an attribute.
    resp.reason = reason
    return HttpError(resp, json.dumps({"error": {"code": status}}).encode("utf-8"))


def _make_script(job_id: str = "job-yt-test") -> Script:
    return Script(
        job_id=job_id,
        title="Test Short",
        description="Legal description with #Shorts tag already included",
        tags=["test", "shorts"],
        narration="This is the test narration for the short video.",
        scenes=[
            Scene(id=1, scene_text="a", image_prompt="p1", duration_seconds=2),
            Scene(id=2, scene_text="b", image_prompt="p2", duration_seconds=2),
        ],
    )


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def patched_env(monkeypatch, tmp_path: Path):
    """Fake OAuth credentials + redirected artifact dirs so no real API is hit."""
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "fake-refresh")
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "fake-client-secret")

    generated = tmp_path / "generated"
    rendered = tmp_path / "rendered"
    monkeypatch.setattr("app.utils.filepaths.GENERATED_DIR", generated)
    monkeypatch.setattr("app.utils.filepaths.RENDERED_DIR", rendered)
    return generated


@pytest.fixture
def video_file(tmp_path: Path) -> Path:
    """A non-empty placeholder MP4 (content doesn't matter — upload is mocked)."""
    path = tmp_path / "final.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 1024)
    return path


@pytest.fixture
def no_backoff(monkeypatch):
    """Zero out tenacity's sleep so retry-based tests finish instantly."""
    monkeypatch.setattr(time, "sleep", lambda _s: None)


def _install_fake_build(
    monkeypatch,
    queue: list[Any],
    *,
    channels_payload: dict | None = None,
) -> tuple[_FakeVideos, _FakeChannels | None, list[dict]]:
    """Patch `build` in youtube_service to return a scripted fake client.

    Returns (videos_stub, channels_stub, build_calls_list) so tests can
    inspect what was passed to `videos().insert(...)` and `build(...)`.
    """
    request = _FakeRequest(queue)
    videos = _FakeVideos(request)
    channels = _FakeChannels(channels_payload) if channels_payload is not None else None
    youtube = _FakeYoutube(videos, channels)
    build_calls: list[dict] = []

    def fake_build(service, version, **kwargs):
        build_calls.append({"service": service, "version": version, **kwargs})
        return youtube

    monkeypatch.setattr("app.services.youtube_service.build", fake_build)
    return videos, channels, build_calls


# --------------------------------------------------------------------------- #
# Tests: happy path + body shape
# --------------------------------------------------------------------------- #

def test_happy_path_uploads_and_writes_record(monkeypatch, patched_env, video_file):
    from app.services import youtube_service
    from app.utils import filepaths

    videos, _, build_calls = _install_fake_build(
        monkeypatch,
        queue=[(None, {"id": "vid_abc123"})],
    )

    script = _make_script("job-happy")
    video_id = youtube_service.upload_short(video_file, script)

    assert video_id == "vid_abc123"

    # `build` got the expected service + version + cache flag.
    assert len(build_calls) == 1
    assert build_calls[0]["service"] == "youtube"
    assert build_calls[0]["version"] == "v3"
    assert build_calls[0]["cache_discovery"] is False

    # videos().insert body shape is correct.
    kwargs = videos.insert_kwargs
    assert kwargs is not None
    assert kwargs["part"] == "snippet,status"
    body = kwargs["body"]
    assert body["snippet"]["title"] == "Test Short"
    assert "#Shorts" in body["snippet"]["description"]
    assert body["snippet"]["categoryId"] == "24"
    assert body["snippet"]["tags"] == ["test", "shorts"]
    assert body["status"]["privacyStatus"] == "private"
    assert body["status"]["selfDeclaredMadeForKids"] is False

    # Upload record was persisted with the right keys.
    record_path = filepaths.upload_record_path("job-happy")
    assert record_path.exists()
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["video_id"] == "vid_abc123"
    assert record["privacy_status"] == "private"
    assert record["title"] == "Test Short"
    assert "uploaded_at" in record


def test_privacy_override_wins_over_config(monkeypatch, patched_env, video_file):
    from app.services import youtube_service

    videos, _, _ = _install_fake_build(
        monkeypatch, queue=[(None, {"id": "vid_unlisted"})]
    )
    video_id = youtube_service.upload_short(
        video_file, _make_script("job-unlisted"), privacy_status="unlisted"
    )

    assert video_id == "vid_unlisted"
    assert videos.insert_kwargs["body"]["status"]["privacyStatus"] == "unlisted"


def test_invalid_privacy_raises(monkeypatch, patched_env, video_file):
    from app.services import youtube_service
    from app.services.youtube_service import YouTubeUploadError

    # No need to install a fake build — we should fail before touching the API.
    with pytest.raises(YouTubeUploadError, match="invalid privacy_status"):
        youtube_service.upload_short(
            video_file, _make_script("job-badpriv"), privacy_status="foo"
        )


def test_missing_file_raises_before_api(monkeypatch, patched_env, tmp_path):
    from app.services import youtube_service
    from app.services.youtube_service import YouTubeUploadError

    build_called = {"n": 0}

    def fake_build(*args, **kwargs):
        build_called["n"] += 1
        raise AssertionError("build() should not be called when the file is missing")

    monkeypatch.setattr("app.services.youtube_service.build", fake_build)

    missing = tmp_path / "does_not_exist.mp4"
    with pytest.raises(YouTubeUploadError, match="video not found"):
        youtube_service.upload_short(missing, _make_script("job-missing"))
    assert build_called["n"] == 0


def test_zero_byte_file_raises(monkeypatch, patched_env, tmp_path):
    from app.services import youtube_service
    from app.services.youtube_service import YouTubeUploadError

    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    monkeypatch.setattr(
        "app.services.youtube_service.build",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("build() should not be called for 0-byte file")
        ),
    )

    with pytest.raises(YouTubeUploadError, match="0 bytes"):
        youtube_service.upload_short(empty, _make_script("job-empty"))


# --------------------------------------------------------------------------- #
# Tests: retry behaviour
# --------------------------------------------------------------------------- #

def test_transient_503_is_retried(monkeypatch, patched_env, video_file, no_backoff):
    from app.services import youtube_service

    queue = [
        _make_http_error(503, "Service Unavailable"),
        _make_http_error(503, "Service Unavailable"),
        (None, {"id": "vid_after_retry"}),
    ]
    videos, _, _ = _install_fake_build(monkeypatch, queue=queue)

    video_id = youtube_service.upload_short(video_file, _make_script("job-retry"))
    assert video_id == "vid_after_retry"
    # Three calls total: 2 failures + 1 success.
    assert videos._request.calls == 3


def test_permanent_403_is_not_retried(monkeypatch, patched_env, video_file, no_backoff):
    from app.services import youtube_service
    from app.services.youtube_service import YouTubeUploadError

    queue = [_make_http_error(403, "Forbidden")]
    videos, _, _ = _install_fake_build(monkeypatch, queue=queue)

    with pytest.raises(YouTubeUploadError, match="403"):
        youtube_service.upload_short(video_file, _make_script("job-403"))

    # Exactly ONE call — no retries on non-transient statuses.
    assert videos._request.calls == 1


# --------------------------------------------------------------------------- #
# Tests: hashtag helper (defensive coverage)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "description,expected",
    [
        ("plain text", "plain text #Shorts"),
        ("already has #Shorts here", "already has #Shorts here"),
        ("lowercase tag #shorts", "lowercase tag #shorts"),
        ("upper tag #SHORTS", "upper tag #SHORTS"),
    ],
)
def test_ensure_shorts_hashtag(description, expected):
    from app.services.youtube_service import _ensure_shorts_hashtag

    assert _ensure_shorts_hashtag(description) == expected


# --------------------------------------------------------------------------- #
# Tests: multi-chunk upload logs progress
# --------------------------------------------------------------------------- #

def test_multi_chunk_upload_logs_progress(
    monkeypatch, patched_env, video_file, capsys
):
    from app.services import youtube_service

    queue = [
        (_FakeStatus(0.5), None),
        (None, {"id": "vid_xyz"}),
    ]
    _install_fake_build(monkeypatch, queue=queue)

    video_id = youtube_service.upload_short(video_file, _make_script("job-progress"))
    assert video_id == "vid_xyz"

    captured = capsys.readouterr().out
    assert "50%" in captured


# --------------------------------------------------------------------------- #
# Tests: verify_credentials
# --------------------------------------------------------------------------- #

def test_verify_credentials_refreshes_access_token(monkeypatch, patched_env):
    """verify_credentials() must refresh the access token and NOT hit any API.

    The pipeline only holds the `youtube.upload` scope, so calling a data
    endpoint like `channels.list(mine=True)` would fail with 403. We only
    validate that the refresh_token itself is still usable.
    """
    from app.services import youtube_service

    calls = {"refresh": 0}

    class FakeCreds:
        def __init__(self, *args, **kwargs):
            self.token = None
            self.scopes = kwargs.get("scopes")

        def refresh(self, request):
            calls["refresh"] += 1
            self.token = "fake-access-token"

    monkeypatch.setattr(youtube_service, "Credentials", FakeCreds)

    info = youtube_service.verify_credentials()
    assert calls["refresh"] == 1
    assert info["token_refreshed"] is True
    assert "https://www.googleapis.com/auth/youtube.upload" in info["scopes"]


def test_verify_credentials_raises_on_bad_refresh_token(monkeypatch, patched_env):
    from app.services import youtube_service
    from app.services.youtube_service import YouTubeUploadError

    class BrokenCreds:
        def __init__(self, *args, **kwargs):
            pass

        def refresh(self, request):
            raise RuntimeError("invalid_grant: Token has been expired or revoked")

    monkeypatch.setattr(youtube_service, "Credentials", BrokenCreds)

    with pytest.raises(YouTubeUploadError, match="invalid_grant"):
        youtube_service.verify_credentials()
