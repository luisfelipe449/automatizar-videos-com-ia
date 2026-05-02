"""Tests for `app.services.video_service` — zero live API or network calls."""

from __future__ import annotations

from pathlib import Path

import pytest
import requests

from app.models import Scene, Script


def _make_script(job_id: str = "hook-test") -> Script:
    return Script(
        job_id=job_id,
        title="Hook test",
        description="Teste hook #Shorts",
        tags=["teste"],
        narration="Narração de teste.",
        scenes=[
            Scene(id=1, scene_text="C1", image_prompt="prompt um", duration_seconds=3),
            Scene(id=2, scene_text="C2", image_prompt="prompt dois", duration_seconds=3),
            Scene(id=3, scene_text="C3", image_prompt="prompt três", duration_seconds=3),
        ],
    )


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


def test_generate_hook_clip_happy_path(monkeypatch, patched_env, no_backoff, tmp_path: Path):
    """fal.ai succeeds → scene_01.mp4 written, hash = raw image_prompt hash."""
    from app.services import video_service
    from app.utils import cache, filepaths

    # Minimal valid MP4 (ffprobe-probeable content faked via monkeypatch below)
    fake_mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2000

    state = _patch_fal_subscribe(monkeypatch, [_fake_fal_result("https://fal/fake.mp4")])

    def fake_get(url, timeout=None, stream=False):
        assert url == "https://fal/fake.mp4"
        return _FakeStreamResponse(status_code=200, content=fake_mp4)

    monkeypatch.setattr("app.services.video_service.requests.get", fake_get)
    # Skip ffprobe validation — not testing ffmpeg here.
    monkeypatch.setattr(
        "app.services.video_service.probe_duration", lambda _p: 5.0
    )

    script = _make_script(job_id="hook-happy")
    result = video_service.generate_hook_clip(script)

    assert result.suffix == ".mp4"
    assert result.name == "scene_01.mp4"
    assert result.exists()

    # Cache hash must be over the RAW image_prompt, not the augmented one.
    hash_file = filepaths.scene_video_hash_path("hook-happy", 1)
    assert hash_file.read_text(encoding="utf-8").strip() == cache.hash_text("prompt um")

    # fal.ai was called with the AUGMENTED prompt (raw + suffix).
    assert len(state["calls"]) == 1
    assert state["calls"][0]["arguments"]["prompt"] == (
        "prompt um" + video_service._HOOK_PROMPT_SUFFIX
    )


def test_generate_hook_clip_falls_back_on_payment_error(monkeypatch, patched_env, no_backoff, tmp_path: Path):
    """fal.ai raises with 'payment' → fallback to visual_service PNG."""
    from app.services import video_service
    from app.utils import filepaths

    def raising_subscribe(endpoint, arguments=None, **kwargs):
        raise RuntimeError("402 payment required")

    monkeypatch.setattr("app.services.video_service.fal_client.subscribe", raising_subscribe)

    # Fake visual_service.generate_scene_image to avoid real Pollinations calls.
    png_path = filepaths.scene_image_path("hook-payment", 1)

    def fake_image(prompt, job_id, scene_id):
        png_path.parent.mkdir(parents=True, exist_ok=True)
        png_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        return png_path

    monkeypatch.setattr(
        "app.services.video_service.visual_service.generate_scene_image",
        fake_image,
    )

    script = _make_script(job_id="hook-payment")
    result = video_service.generate_hook_clip(script)

    assert result.suffix == ".png"
    assert result == png_path
    assert result.exists()

    # No leftover .mp4 artifact on disk.
    mp4_path = filepaths.scene_video_path("hook-payment", 1)
    assert not mp4_path.exists()


def test_generate_hook_clip_falls_back_when_fal_key_missing(monkeypatch, tmp_path: Path):
    """Missing FAL_KEY → immediate fallback to visual_service PNG (no fal.ai retry)."""
    # Manually isolate dirs (we're NOT using `patched_env` here because it sets FAL_KEY).
    generated = tmp_path / "generated"
    rendered = tmp_path / "rendered"
    monkeypatch.setattr("app.utils.filepaths.GENERATED_DIR", generated)
    monkeypatch.setattr("app.utils.filepaths.RENDERED_DIR", rendered)
    monkeypatch.delenv("FAL_KEY", raising=False)

    from app.utils import quota
    quota._unavailable.clear()

    from app.services import video_service
    from app.utils import filepaths

    # fal_client.subscribe must NOT be called — assert by failing if it is.
    called = {"subscribe": 0}

    def forbidden_subscribe(*args, **kwargs):
        called["subscribe"] += 1
        raise AssertionError("fal.ai must not be called when FAL_KEY is missing")

    monkeypatch.setattr("app.services.video_service.fal_client.subscribe", forbidden_subscribe)

    png_path = filepaths.scene_image_path("hook-nokey", 1)

    def fake_image(prompt, job_id, scene_id):
        png_path.parent.mkdir(parents=True, exist_ok=True)
        png_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        return png_path

    monkeypatch.setattr(
        "app.services.video_service.visual_service.generate_scene_image",
        fake_image,
    )

    script = _make_script(job_id="hook-nokey")
    result = video_service.generate_hook_clip(script)

    assert result.suffix == ".png"
    assert called["subscribe"] == 0


def test_generate_hook_clip_cache_hit(monkeypatch, patched_env, tmp_path: Path):
    """Pre-seeded MP4 + matching hash → no fal.ai call."""
    from app.services import video_service
    from app.utils import cache, filepaths

    job_id = "hook-cached"
    filepaths.ensure_job_dirs(job_id)

    mp4_path = filepaths.scene_video_path(job_id, 1)
    hash_path = filepaths.scene_video_hash_path(job_id, 1)
    mp4_path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2000)
    cache.write_hash_file(hash_path, cache.hash_text("prompt um"))

    def forbidden_subscribe(*args, **kwargs):
        raise AssertionError("fal.ai must not be called on cache hit")

    monkeypatch.setattr("app.services.video_service.fal_client.subscribe", forbidden_subscribe)

    script = _make_script(job_id=job_id)
    result = video_service.generate_hook_clip(script)

    assert result == mp4_path


def test_generate_hook_plus_images_returns_mixed_paths(monkeypatch, patched_env, no_backoff, tmp_path: Path):
    """Hook succeeds + 2 image scenes → result is [.mp4, .png, .png] in order."""
    from app.services import video_service
    from app.utils import filepaths

    fake_mp4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2000
    _patch_fal_subscribe(monkeypatch, [_fake_fal_result("https://fal/clip.mp4")])

    def fake_get(url, timeout=None, stream=False):
        return _FakeStreamResponse(status_code=200, content=fake_mp4)

    monkeypatch.setattr("app.services.video_service.requests.get", fake_get)
    monkeypatch.setattr(
        "app.services.video_service.probe_duration", lambda _p: 5.0
    )

    # Mock image generation for scenes 2, 3.
    def fake_image(prompt, job_id, scene_id):
        path = filepaths.scene_image_path(job_id, scene_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        return path

    monkeypatch.setattr(
        "app.services.video_service.visual_service.generate_scene_image",
        fake_image,
    )

    script = _make_script(job_id="hook-mixed")
    result = video_service.generate_hook_plus_images(script)

    assert len(result) == 3
    assert result[0].suffix == ".mp4"
    assert result[0].name == "scene_01.mp4"
    assert result[1].suffix == ".png"
    assert result[1].name == "scene_02.png"
    assert result[2].suffix == ".png"
    assert result[2].name == "scene_03.png"
