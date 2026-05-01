"""Tests for `app.services.video_service` — zero live API or network calls."""

from __future__ import annotations

from pathlib import Path

import pytest
import requests


# --------------------------------------------------------------------------- #
# Fake fal.ai response + streaming requests helpers
# --------------------------------------------------------------------------- #

class _FakeStreamResponse:
    """Minimal `requests.Response`-shaped object for `requests.get(..., stream=True)`."""

    def __init__(self, *, status_code: int, content: bytes):
        self.status_code = status_code
        self._content = content

    def raise_for_status(self) -> None:
        if 400 <= self.status_code < 600:
            err = requests.exceptions.HTTPError(f"{self.status_code}")
            err.response = self  # type: ignore[attr-defined]
            raise err

    def iter_content(self, chunk_size: int = 64 * 1024):
        # Yield in a single chunk — tests don't need real streaming.
        if self._content:
            yield self._content


def _fake_fal_result(url: str = "https://fal.ai/fake.mp4") -> dict:
    """Shape matches the live fal.ai ltx-video response (confirmed via live probe)."""
    return {
        "video": {
            "url": url,
            "content_type": "video/mp4",
            "file_size": 1234,
        },
        "seed": 42,
    }


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def patched_env(monkeypatch, tmp_path: Path):
    """Isolate GENERATED_DIR, set FAL_KEY, reset quota state."""
    monkeypatch.setenv("FAL_KEY", "fake-key-for-tests")
    generated = tmp_path / "generated"
    rendered = tmp_path / "rendered"
    monkeypatch.setattr("app.utils.filepaths.GENERATED_DIR", generated)
    monkeypatch.setattr("app.utils.filepaths.RENDERED_DIR", rendered)

    from app.utils import quota

    quota._unavailable.clear()
    return generated


@pytest.fixture
def no_backoff(monkeypatch):
    """Zero-cost tenacity sleeps so retries don't slow tests."""
    import time

    monkeypatch.setattr(time, "sleep", lambda _s: None)


def _patch_fal_subscribe(monkeypatch, responses):
    """Scripted fake for `fal_client.subscribe(...)`."""
    state = {"responses": list(responses), "calls": []}

    def fake_subscribe(endpoint, arguments=None, **kwargs):
        state["calls"].append({"endpoint": endpoint, "arguments": arguments})
        if not state["responses"]:
            raise RuntimeError("fake fal.ai exhausted")
        item = state["responses"].pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(
        "app.services.video_service.fal_client.subscribe", fake_subscribe
    )
    return state


def _patch_requests_get(monkeypatch, responses):
    state = {"responses": list(responses), "calls": []}

    def fake_get(url, **kwargs):
        state["calls"].append({"url": url, "kwargs": kwargs})
        if not state["responses"]:
            raise RuntimeError("fake requests exhausted")
        item = state["responses"].pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr("app.services.video_service.requests.get", fake_get)
    return state


def _patch_validate_mp4(monkeypatch, side_effect=None):
    """Patch `_validate_mp4` so tests don't need real ffprobe."""
    calls: list[Path] = []

    def fake_validate(path: Path) -> None:
        calls.append(path)
        if callable(side_effect):
            side_effect(path)

    monkeypatch.setattr("app.services.video_service._validate_mp4", fake_validate)
    return calls


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

def test_moderation_blocks_prompt_before_touching_fal(patched_env, monkeypatch):
    """A blocklisted term must raise before any fal.ai / requests call."""
    from app.services.video_service import generate_scene_clip
    from app.services.visual_service import VisualGenerationError

    # Any fal.ai call in this test must be loud about being wrong.
    fal_state = _patch_fal_subscribe(monkeypatch, [RuntimeError("should not be called")])
    req_state = _patch_requests_get(monkeypatch, [])

    with pytest.raises(VisualGenerationError, match="local moderation"):
        generate_scene_clip("explicit violence scene", "job-mod", 1)

    # Neither mocked layer was touched.
    assert fal_state["calls"] == []
    assert req_state["calls"] == []


def test_happy_path_writes_mp4_and_hash_then_cache_hits(monkeypatch, patched_env):
    from app.services.video_service import generate_scene_clip
    from app.utils import filepaths

    payload = b"FAKEMP4CONTENT" + b"\x00" * 2048
    fal_state = _patch_fal_subscribe(monkeypatch, [_fake_fal_result()])
    req_state = _patch_requests_get(
        monkeypatch, [_FakeStreamResponse(status_code=200, content=payload)]
    )
    _patch_validate_mp4(monkeypatch)

    path = generate_scene_clip("a serene neon forest", "job-hp", 1)

    assert path == filepaths.scene_video_path("job-hp", 1)
    assert path.exists()
    assert path.read_bytes() == payload
    assert filepaths.scene_video_hash_path("job-hp", 1).exists()
    assert len(fal_state["calls"]) == 1
    assert fal_state["calls"][0]["endpoint"] == "fal-ai/ltx-video"
    assert fal_state["calls"][0]["arguments"] == {"prompt": "a serene neon forest"}
    assert len(req_state["calls"]) == 1

    # Second call must cache-hit — fal.ai and requests untouched.
    path2 = generate_scene_clip("a serene neon forest", "job-hp", 1)
    assert path2 == path
    assert len(fal_state["calls"]) == 1
    assert len(req_state["calls"]) == 1


def test_fal_api_failure_falls_back_to_image(monkeypatch, patched_env, no_backoff):
    """ANY fal.ai exception must trigger the visual_service PNG fallback."""
    from app.services import video_service
    from app.utils import filepaths

    fake_png = filepaths.scene_image_path("job-fb", 1)

    def fake_generate_image(prompt, job_id, scene_id):
        fake_png.parent.mkdir(parents=True, exist_ok=True)
        fake_png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 1024)
        return fake_png

    monkeypatch.setattr(
        video_service.visual_service, "generate_scene_image", fake_generate_image
    )
    # 3 transient exceptions: default_retry gives up after all attempts,
    # then the caller falls back to the PNG path.
    _patch_fal_subscribe(
        monkeypatch,
        [ConnectionError("fal down") for _ in range(3)],
    )

    result = video_service.generate_scene_clip("moody skyline", "job-fb", 1)

    assert result == fake_png
    assert result.suffix == ".png"
    # No mp4 artifact nor its hash should be left behind.
    assert not filepaths.scene_video_path("job-fb", 1).exists()
    assert not filepaths.scene_video_hash_path("job-fb", 1).exists()


def test_malformed_response_falls_back_to_image(monkeypatch, patched_env):
    """A response with no `video.url` key is malformed; fall back to PNG."""
    from app.services import video_service
    from app.utils import filepaths

    fake_png = filepaths.scene_image_path("job-mal", 1)

    def fake_generate_image(prompt, job_id, scene_id):
        fake_png.parent.mkdir(parents=True, exist_ok=True)
        fake_png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 1024)
        return fake_png

    monkeypatch.setattr(
        video_service.visual_service, "generate_scene_image", fake_generate_image
    )
    # Response shape is wrong — `video` key is missing.
    _patch_fal_subscribe(monkeypatch, [{"seed": 1}])

    result = video_service.generate_scene_clip("sunrise hills", "job-mal", 1)
    assert result == fake_png


def test_quota_error_marks_video_unavailable(monkeypatch, patched_env, no_backoff):
    """A 429-shaped fal.ai error marks quota.is_video_unavailable for the job."""
    from app.services import video_service
    from app.utils import filepaths, quota

    fake_png = filepaths.scene_image_path("job-qe", 1)

    def fake_generate_image(prompt, job_id, scene_id):
        fake_png.parent.mkdir(parents=True, exist_ok=True)
        fake_png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 1024)
        return fake_png

    monkeypatch.setattr(
        video_service.visual_service, "generate_scene_image", fake_generate_image
    )
    # "429 rate limit" is retryable per the predicate — default_retry will
    # burn all 3 attempts. Each attempt raises the same thing, and the
    # last one hits `_is_quota_error` which flips the quota bit.
    _patch_fal_subscribe(
        monkeypatch,
        [RuntimeError("429 rate limit exceeded") for _ in range(3)],
    )

    result = video_service.generate_scene_clip("quota test prompt", "job-qe", 1)
    assert result == fake_png
    assert quota.is_video_unavailable("job-qe") is True


def test_missing_fal_key_short_circuits_to_fallback(monkeypatch, patched_env):
    """Without FAL_KEY the service must not even attempt fal.ai."""
    from app.services import video_service
    from app.utils import filepaths, quota

    monkeypatch.delenv("FAL_KEY", raising=False)
    fake_png = filepaths.scene_image_path("job-nk", 1)

    def fake_generate_image(prompt, job_id, scene_id):
        fake_png.parent.mkdir(parents=True, exist_ok=True)
        fake_png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 1024)
        return fake_png

    monkeypatch.setattr(
        video_service.visual_service, "generate_scene_image", fake_generate_image
    )
    # If fal.ai IS called, this test MUST fail loudly.
    fal_state = _patch_fal_subscribe(
        monkeypatch, [RuntimeError("subscribe must not be reached")]
    )

    result = video_service.generate_scene_clip("missing key", "job-nk", 1)

    assert result == fake_png
    assert fal_state["calls"] == []
    assert quota.is_video_unavailable("job-nk") is True


def test_generate_all_scene_clips_preserves_order(monkeypatch, patched_env):
    """Parallel generation must return paths in scene order regardless of completion order."""
    from app.models import Scene, Script
    from app.services.video_service import generate_all_scene_clips
    from app.utils import filepaths

    payload = b"FAKEMP4" + b"\x00" * 2048
    # Enough fake results + downloads for 3 scenes.
    _patch_fal_subscribe(
        monkeypatch,
        [
            _fake_fal_result("https://fal.ai/a.mp4"),
            _fake_fal_result("https://fal.ai/b.mp4"),
            _fake_fal_result("https://fal.ai/c.mp4"),
        ],
    )
    _patch_requests_get(
        monkeypatch,
        [_FakeStreamResponse(status_code=200, content=payload) for _ in range(3)],
    )
    _patch_validate_mp4(monkeypatch)

    script = Script(
        job_id="job-order",
        title="Test",
        description="Desc #Shorts",
        tags=["x"],
        narration="Texto.",
        scenes=[
            Scene(id=1, scene_text="s1", image_prompt="alpha prompt", duration_seconds=5),
            Scene(id=2, scene_text="s2", image_prompt="bravo prompt", duration_seconds=5),
            Scene(id=3, scene_text="s3", image_prompt="charlie prompt", duration_seconds=5),
        ],
    )

    paths = generate_all_scene_clips(script)

    assert paths == [
        filepaths.scene_video_path("job-order", 1),
        filepaths.scene_video_path("job-order", 2),
        filepaths.scene_video_path("job-order", 3),
    ]
    for p in paths:
        assert p.exists()


def test_generate_all_scene_clips_mixed_success_and_fallback(
    monkeypatch, patched_env, no_backoff
):
    """Some scenes succeed (.mp4), others fall back (.png) — order preserved."""
    from app.models import Scene, Script
    from app.services import video_service
    from app.utils import filepaths

    payload = b"FAKEMP4" + b"\x00" * 2048

    # Scene 1 & 3: success. Scene 2: fal.ai fails → PNG fallback.
    # Order is non-deterministic across threads, so we script fal_client
    # by prompt content rather than call order.
    prompt_to_result = {
        "alpha prompt": _fake_fal_result("https://fal.ai/a.mp4"),
        "charlie prompt": _fake_fal_result("https://fal.ai/c.mp4"),
    }

    def fake_subscribe(endpoint, arguments=None, **kwargs):
        prompt = (arguments or {}).get("prompt")
        if prompt in prompt_to_result:
            return prompt_to_result[prompt]
        raise ConnectionError(f"simulated failure for {prompt!r}")

    monkeypatch.setattr(
        "app.services.video_service.fal_client.subscribe", fake_subscribe
    )

    # Each scene's download returns fresh bytes. Use a counter to dispense
    # infinite responses — ordering here is irrelevant because only the two
    # successful scenes trigger `requests.get`.
    def fake_get(url, **kwargs):
        return _FakeStreamResponse(status_code=200, content=payload)

    monkeypatch.setattr("app.services.video_service.requests.get", fake_get)
    _patch_validate_mp4(monkeypatch)

    # PNG fallback: write a real scene image.
    def fake_generate_image(prompt, job_id, scene_id):
        png = filepaths.scene_image_path(job_id, scene_id)
        png.parent.mkdir(parents=True, exist_ok=True)
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 1024)
        return png

    monkeypatch.setattr(
        video_service.visual_service,
        "generate_scene_image",
        fake_generate_image,
    )

    script = Script(
        job_id="job-mixed",
        title="Test",
        description="Desc #Shorts",
        tags=["x"],
        narration="Texto.",
        scenes=[
            Scene(id=1, scene_text="s1", image_prompt="alpha prompt", duration_seconds=5),
            Scene(id=2, scene_text="s2", image_prompt="bravo prompt", duration_seconds=5),
            Scene(id=3, scene_text="s3", image_prompt="charlie prompt", duration_seconds=5),
        ],
    )

    paths = video_service.generate_all_scene_clips(script)

    assert len(paths) == 3
    # Order preserved, suffixes dispatch by per-scene outcome.
    assert paths[0] == filepaths.scene_video_path("job-mixed", 1)
    assert paths[1] == filepaths.scene_image_path("job-mixed", 2)  # fallback
    assert paths[2] == filepaths.scene_video_path("job-mixed", 3)
    assert paths[0].suffix == ".mp4"
    assert paths[1].suffix == ".png"
    assert paths[2].suffix == ".mp4"
