"""Tests for `app.services.visual_service` — no real network / API ever contacted."""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from PIL import Image


# --------------------------------------------------------------------------- #
# Fake PNG bytes (a real, valid 1×1 PNG decoded by PIL)
# --------------------------------------------------------------------------- #

def _make_png_bytes(min_size: int = 1200) -> bytes:
    """Return valid PNG bytes comfortably above visual_service's ≥1000 B floor.

    PIL's solid-color PNG compresses to ~150 B, which the sanity check would
    reject. Filling with random-ish noise defeats zlib and reliably produces
    a multi-kB payload while staying a real, decodable PNG.
    """
    import os as _os

    img = Image.frombytes("RGB", (64, 64), _os.urandom(64 * 64 * 3))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    data = buf.getvalue()
    assert len(data) >= min_size, f"PNG generator produced only {len(data)} bytes"
    return data


_FAKE_PNG = _make_png_bytes()
_FAKE_IMAGEN_PNG = _make_png_bytes()  # distinct Python object; same shape


# --------------------------------------------------------------------------- #
# Fake requests.get response
# --------------------------------------------------------------------------- #

class _FakeResponse:
    def __init__(self, *, status_code: int, content: bytes, content_type: str = "image/png"):
        self.status_code = status_code
        self.content = content
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self) -> None:
        """Mirror `requests.Response.raise_for_status` for 4xx/5xx statuses."""
        if 400 <= self.status_code < 600:
            err = requests.exceptions.HTTPError(f"{self.status_code} HTTP error")
            err.response = self  # type: ignore[attr-defined]
            raise err


# --------------------------------------------------------------------------- #
# Fake Gemini Imagen SDK
# --------------------------------------------------------------------------- #

def _make_imagen_response(image_bytes: bytes):
    image = SimpleNamespace(image_bytes=image_bytes)
    generated = SimpleNamespace(image=image)
    return SimpleNamespace(generated_images=[generated])


class _FakeImagenModels:
    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def generate_images(self, **kwargs):  # noqa: D401 — SDK-shaped signature
        self.calls.append(kwargs)
        if not self._responses:
            raise RuntimeError("FakeImagenModels exhausted")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, bytes):
            return _make_imagen_response(item)
        return item


class FakeGenaiClient:
    """Class-level shared queue so every constructed client drains the same list."""

    responses: list = []
    calls: list[dict] = []
    last_instance: "FakeGenaiClient | None" = None

    def __init__(self, *, api_key: str):
        self.api_key = api_key
        self.models = _FakeImagenModels.__new__(_FakeImagenModels)
        self.models._responses = type(self).responses
        self.models.calls = type(self).calls
        FakeGenaiClient.last_instance = self


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

@pytest.fixture
def patched_env(monkeypatch, tmp_path: Path):
    """Fake GEMINI_API_KEY, isolated quota state, redirected generated/rendered dirs.

    Also pins IMAGE_PROVIDER=pollinations so the test assumptions about
    primary/fallback order don't depend on the developer's local .env.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr("app.config.IMAGE_PROVIDER", "pollinations")
    generated = tmp_path / "generated"
    rendered = tmp_path / "rendered"
    monkeypatch.setattr("app.utils.filepaths.GENERATED_DIR", generated)
    monkeypatch.setattr("app.utils.filepaths.RENDERED_DIR", rendered)

    # Reset the in-process quota flags between tests so state doesn't leak.
    from app.utils import quota

    quota._unavailable.clear()  # pragma: no cover - test-only cleanup
    return generated


@pytest.fixture
def no_backoff(monkeypatch):
    """Kill tenacity sleep so retry-based failure paths finish instantly."""
    import time

    monkeypatch.setattr(time, "sleep", lambda _s: None)


def _install_fake_requests(monkeypatch, responses):
    """Install a scripted `requests.get` mock inside visual_service."""
    state = {"responses": list(responses), "calls": []}

    def fake_get(url, **kwargs):
        state["calls"].append({"url": url, "kwargs": kwargs})
        if not state["responses"]:
            raise RuntimeError("fake_requests exhausted")
        item = state["responses"].pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr("app.services.visual_service.requests.get", fake_get)
    return state


def _install_fake_genai(monkeypatch, responses):
    FakeGenaiClient.responses = list(responses)
    FakeGenaiClient.calls = []
    FakeGenaiClient.last_instance = None
    # visual_service imports `genai` eagerly, so swap the Client factory in-place.
    monkeypatch.setattr("app.services.visual_service.genai.Client", FakeGenaiClient)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

def test_moderation_blocks_prompt(patched_env):
    from app.services.visual_service import VisualGenerationError, generate_scene_image

    with pytest.raises(VisualGenerationError, match="local moderation"):
        generate_scene_image("explicit violence scene", job_id="job-mod", scene_id=1)


def test_pollinations_happy_path_and_cache(monkeypatch, patched_env):
    from app.services.visual_service import generate_scene_image
    from app.utils import filepaths

    state = _install_fake_requests(
        monkeypatch,
        [_FakeResponse(status_code=200, content=_FAKE_PNG, content_type="image/png")],
    )

    path = generate_scene_image("cinematic neon city, 9:16", job_id="job-poll", scene_id=1)

    assert path == filepaths.scene_image_path("job-poll", 1)
    assert path.exists()
    assert path.read_bytes() == _FAKE_PNG
    assert filepaths.scene_image_hash_path("job-poll", 1).exists()
    assert len(state["calls"]) == 1
    # URL contains encoded prompt and dimensions.
    url = state["calls"][0]["url"]
    assert "width=1080" in url and "height=1920" in url
    assert "nologo=true" in url
    assert "model=flux" in url
    assert "seed=" in url

    # Second invocation → cache hit; requests.get must NOT be called again.
    path2 = generate_scene_image("cinematic neon city, 9:16", job_id="job-poll", scene_id=1)
    assert path2 == path
    assert len(state["calls"]) == 1  # unchanged


def test_pollinations_failure_falls_through_to_imagen(monkeypatch, patched_env, no_backoff):
    from app.services.visual_service import generate_scene_image
    from app.utils import filepaths, quota

    # Pollinations: connection error on every retry (network_retry will attempt 5).
    _install_fake_requests(
        monkeypatch,
        [requests.exceptions.ConnectionError("no route") for _ in range(5)],
    )
    _install_fake_genai(monkeypatch, [_FAKE_IMAGEN_PNG])

    path = generate_scene_image("serene forest at sunrise", job_id="job-fb", scene_id=2)

    assert path == filepaths.scene_image_path("job-fb", 2)
    assert path.exists()
    assert path.read_bytes() == _FAKE_IMAGEN_PNG
    assert quota.is_pollinations_unavailable("job-fb") is True
    assert len(FakeGenaiClient.calls) == 1
    # Imagen was given the right model + prompt.
    kwargs = FakeGenaiClient.last_instance.models.calls[0]
    assert kwargs["prompt"] == "serene forest at sunrise"


def test_both_providers_fail_raises_and_skips_hash(monkeypatch, patched_env, no_backoff):
    from app.services.visual_service import VisualGenerationError, generate_scene_image
    from app.utils import filepaths

    _install_fake_requests(
        monkeypatch,
        [requests.exceptions.ConnectionError("no route") for _ in range(5)],
    )
    _install_fake_genai(monkeypatch, [RuntimeError("imagen blew up")])

    with pytest.raises(VisualGenerationError, match="all visual providers failed"):
        generate_scene_image("calm mountain lake", job_id="job-both-fail", scene_id=1)

    assert not filepaths.scene_image_path("job-both-fail", 1).exists()
    assert not filepaths.scene_image_hash_path("job-both-fail", 1).exists()


def test_no_api_key_means_no_fallback(monkeypatch, patched_env, no_backoff):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    from app.services.visual_service import VisualGenerationError, generate_scene_image

    _install_fake_requests(
        monkeypatch,
        [requests.exceptions.ConnectionError("no route") for _ in range(5)],
    )

    with pytest.raises(VisualGenerationError) as excinfo:
        generate_scene_image("sunset beach", job_id="job-no-key", scene_id=1)
    # Message must mention the missing fallback so operators can diagnose quickly.
    assert "no fallback available" in str(excinfo.value)


def test_corrupt_png_from_pollinations_falls_through(monkeypatch, patched_env, no_backoff):
    from app.services.visual_service import generate_scene_image
    from app.utils import filepaths

    # Pollinations returns 200 + image/png, but the bytes are garbage → header check fails.
    _install_fake_requests(
        monkeypatch,
        [_FakeResponse(status_code=200, content=b"not a png", content_type="image/png")],
    )
    _install_fake_genai(monkeypatch, [_FAKE_IMAGEN_PNG])

    path = generate_scene_image("moody jazz club", job_id="job-corrupt", scene_id=1)

    # The final artifact comes from Imagen, NOT from the corrupt pollinations body.
    assert path.read_bytes() == _FAKE_IMAGEN_PNG
    # And no `.part` file was left behind.
    assert not filepaths.scene_image_path("job-corrupt", 1).with_suffix(".png.part").exists()


def test_pollinations_5xx_is_retried(monkeypatch, patched_env, no_backoff):
    """A 502 followed by a 200 should succeed on retry — raise_for_status kicks in."""
    from app.services.visual_service import generate_scene_image
    from app.utils import filepaths

    state = _install_fake_requests(
        monkeypatch,
        [
            _FakeResponse(status_code=502, content=b"", content_type="text/html"),
            _FakeResponse(status_code=502, content=b"", content_type="text/html"),
            _FakeResponse(status_code=200, content=_FAKE_PNG, content_type="image/png"),
        ],
    )

    path = generate_scene_image("retryable scene", job_id="job-5xx", scene_id=1)

    assert path == filepaths.scene_image_path("job-5xx", 1)
    assert path.read_bytes() == _FAKE_PNG
    # Exactly three GETs: 2 × 502 + 1 × 200.
    assert len(state["calls"]) == 3
    # `stream=True` must no longer be set on the request (Fix 3).
    for call in state["calls"]:
        assert call["kwargs"].get("stream") is not True


def test_generate_all_scene_images_preserves_order(monkeypatch, patched_env):
    from app.models import Scene, Script
    from app.services.visual_service import generate_all_scene_images
    from app.utils import filepaths

    _install_fake_requests(
        monkeypatch,
        [
            _FakeResponse(status_code=200, content=_FAKE_PNG, content_type="image/png"),
            _FakeResponse(status_code=200, content=_FAKE_PNG, content_type="image/png"),
            _FakeResponse(status_code=200, content=_FAKE_PNG, content_type="image/png"),
        ],
    )

    script = Script(
        job_id="job-all",
        title="Test",
        description="Desc #Shorts",
        tags=["a", "b", "c"],
        narration="narração",
        scenes=[
            Scene(id=1, scene_text="s1", image_prompt="p one", duration_seconds=5),
            Scene(id=2, scene_text="s2", image_prompt="p two", duration_seconds=5),
            Scene(id=3, scene_text="s3", image_prompt="p three", duration_seconds=5),
        ],
    )

    paths = generate_all_scene_images(script)

    assert paths == [
        filepaths.scene_image_path("job-all", 1),
        filepaths.scene_image_path("job-all", 2),
        filepaths.scene_image_path("job-all", 3),
    ]
    for p in paths:
        assert p.exists()


# --------------------------------------------------------------------------- #
# Seed range
#
# Pollinations validates `seed` against a signed 32-bit ceiling and rejects
# anything above it — the response is an opaque HTTP 500 whose body hides
# `{"seed": ["Too big: expected number to be <=2147483647"]}`. The seed is
# derived from the prompt, so an out-of-range one fails identically on every
# retry: that scene can never be generated. `int(sha256[:8], 16)` spans the
# full unsigned range, so this hit half of all prompts.
# --------------------------------------------------------------------------- #

_POLLINATIONS_MAX_SEED = 2_147_483_647


def test_deterministic_seed_never_exceeds_pollinations_ceiling():
    from app.services.visual_service import _deterministic_seed

    # Enough prompts that the old unsigned-32-bit version fails with certainty:
    # each has a ~50% chance of landing above the ceiling.
    for i in range(200):
        seed = _deterministic_seed(f"a cinematic vertical shot, variant {i}")
        assert 0 <= seed <= _POLLINATIONS_MAX_SEED, f"variant {i} produced {seed}"


def test_deterministic_seed_is_still_stable_per_prompt():
    """Masking must not break reproducibility — same prompt, same image."""
    from app.services.visual_service import _deterministic_seed

    assert _deterministic_seed("workshop at golden hour") == _deterministic_seed(
        "workshop at golden hour"
    )
    assert _deterministic_seed("prompt A") != _deterministic_seed("prompt B")


def test_known_overflowing_prompt_is_brought_into_range():
    """The exact prompt that failed in production: seed was 2768101696."""
    from app.services.visual_service import _deterministic_seed

    prompt = (
        "A frustrated freelancer looking at a laptop screen with an empty "
        "inbox, construction site background, vertical 9:16, realistic "
        "cinematic lighting"
    )
    assert _deterministic_seed(prompt) <= _POLLINATIONS_MAX_SEED
