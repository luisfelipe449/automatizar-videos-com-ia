"""Pollinations (primary) / Gemini Imagen (fallback) → scene_XX.png.

Single responsibility: given an image prompt + job_id + scene_id, write a 1080×1920
PNG to `filepaths.scene_image_path(...)`. Honors a hash-based cache so reruns are
a no-op. Plan change from original PLANO.md: Pollinations is the PRIMARY (free,
no credentials); Imagen is the fallback, attempted only if Pollinations errors
AND `GEMINI_API_KEY` is set.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
from pathlib import Path
from urllib.parse import quote

import requests
from google import genai
from google.genai import types
from PIL import Image

from app.config import GEMINI_IMAGE_MODEL, TARGET_HEIGHT, TARGET_WIDTH
from app.models import Script
from app.utils import cache, filepaths, quota
from app.utils.retries import default_retry, network_retry

_LOG_PREFIX = "[visual_service]"

# Magic numbers for format sniffing. Pollinations may serve JPEG even when we
# ask for a PNG-named artifact, so we detect both and transcode to PNG below.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
# Anything smaller is definitely corrupt (PNG header + IHDR ≈ 70 bytes; JPEG
# SOI + SOF markers similar). 1KB is still generous for 1080x1920 content.
_MIN_IMAGE_BYTES = 1000

_POLLINATIONS_URL = "https://image.pollinations.ai/prompt/{prompt}"
_POLLINATIONS_TIMEOUT_S = 60
_ACCEPTED_CONTENT_TYPES = ("image/png", "image/jpeg")

# Loud blocklist: Shorts policy violations should fail fast, not be scrubbed.
_BLOCKED_TERMS = {
    "nude",
    "naked",
    "explicit",
    "gore",
    "blood",
    "torture",
    "nsfw",
    "sexual",
    "pornografia",
    "violência gráfica",
}


class VisualGenerationError(RuntimeError):
    """Raised when neither Pollinations nor Imagen can produce a usable image."""


# --------------------------------------------------------------------------- #
# Helpers (pure where possible)
# --------------------------------------------------------------------------- #

def _moderate(image_prompt: str) -> None:
    """Raise VisualGenerationError if `image_prompt` hits the local blocklist."""
    lowered = image_prompt.lower()
    for term in _BLOCKED_TERMS:
        # Word-boundary check so "bloodhound" doesn't match "blood".
        if re.search(rf"\b{re.escape(term)}\b", lowered):
            raise VisualGenerationError(
                f"prompt rejected by local moderation (term: {term!r})"
            )


def _deterministic_seed(image_prompt: str) -> int:
    """First 8 hex chars of sha256(prompt) → int. Keeps Pollinations reproducible."""
    digest = hashlib.sha256(image_prompt.encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def _looks_like_image(data: bytes) -> bool:
    """True iff `data` is large enough AND starts with a known image magic."""
    if len(data) < _MIN_IMAGE_BYTES:
        return False
    return data.startswith(_PNG_MAGIC) or data.startswith(_JPEG_MAGIC)


def _to_png_bytes(data: bytes) -> bytes:
    """Return `data` as PNG bytes. Transcodes JPEG via Pillow when needed."""
    if data.startswith(_PNG_MAGIC):
        return data
    # JPEG (or anything Pillow can decode) → re-encode as PNG so the on-disk
    # artifact always matches its `.png` extension and downstream ffmpeg input.
    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()


def _write_png_safe(data: bytes, out_path: Path) -> None:
    """Validate `data` decodes as an image, transcode to PNG, write atomically.

    Writes to a `.part` sibling first and renames over the target, so a crashed
    write never leaves a half-file that the cache would later consider "fresh".
    """
    if not _looks_like_image(data):
        raise VisualGenerationError(
            f"provider returned {len(data)} bytes without a valid image header"
        )
    try:
        png_bytes = _to_png_bytes(data)
    except Exception as exc:  # noqa: BLE001 — any Pillow decode failure surfaces uniformly
        raise VisualGenerationError(f"could not transcode provider bytes to PNG: {exc}") from exc

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".part")
    tmp.write_bytes(png_bytes)
    tmp.replace(out_path)


@network_retry
def _pollinations_get(url: str) -> requests.Response:
    """Raw GET wrapped by `network_retry` so flaky 5xx/Timeout attempts back off.

    Calls `raise_for_status()` so 5xx / 429 responses surface as
    `requests.exceptions.HTTPError` — which `network_retry`'s predicate
    recognises as transient and will retry with backoff.
    """
    response = requests.get(url, timeout=_POLLINATIONS_TIMEOUT_S)
    response.raise_for_status()
    return response


def _via_pollinations(image_prompt: str) -> bytes:
    """Fetch bytes from Pollinations. Raises VisualGenerationError on failure."""
    encoded = quote(image_prompt, safe="")
    seed = _deterministic_seed(image_prompt)
    url = (
        f"{_POLLINATIONS_URL.format(prompt=encoded)}"
        f"?width={TARGET_WIDTH}&height={TARGET_HEIGHT}"
        f"&nologo=true&model=flux&seed={seed}"
    )
    try:
        response = _pollinations_get(url)
    except requests.exceptions.RequestException as exc:
        raise VisualGenerationError(f"Pollinations network error: {exc}") from exc

    if response.status_code != 200:
        raise VisualGenerationError(
            f"Pollinations returned HTTP {response.status_code}"
        )

    content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip()
    if content_type not in _ACCEPTED_CONTENT_TYPES:
        raise VisualGenerationError(
            f"Pollinations returned unexpected Content-Type: {content_type!r}"
        )

    data = response.content
    if not data:
        raise VisualGenerationError("Pollinations returned empty body")
    return data


@default_retry
def _imagen_generate(client, image_prompt: str) -> bytes:
    """Call Gemini Imagen once (wrapped by default_retry for transient errors)."""
    response = client.models.generate_images(
        model=GEMINI_IMAGE_MODEL,
        prompt=image_prompt,
        config=types.GenerateImagesConfig(
            number_of_images=1,
            aspect_ratio="9:16",
        ),
    )
    try:
        return response.generated_images[0].image.image_bytes
    except (AttributeError, IndexError, TypeError) as exc:
        raise VisualGenerationError(f"Unexpected Imagen response shape: {exc}") from exc


def _via_imagen(image_prompt: str, job_id: str) -> bytes:
    """Fallback path. Raises VisualGenerationError on any failure (+ marks quota)."""
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        quota.mark_imagen_unavailable(job_id)
        raise VisualGenerationError("GEMINI_API_KEY not set; no fallback available")

    client = genai.Client(api_key=api_key)
    try:
        data = _imagen_generate(client, image_prompt)
    except VisualGenerationError:
        raise
    except Exception as exc:  # noqa: BLE001 — quota vs generic failure handled below
        message = str(exc).lower()
        if "429" in message or "resource_exhausted" in message or "quota" in message:
            quota.mark_imagen_unavailable(job_id)
            raise VisualGenerationError(f"Imagen quota exhausted: {exc}") from exc
        raise VisualGenerationError(f"Imagen call failed: {exc}") from exc

    if not data:
        raise VisualGenerationError("Imagen returned empty payload")
    return data


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def generate_scene_image(image_prompt: str, job_id: str, scene_id: int) -> Path:
    """Produce `scene_XX.png` via Pollinations → Imagen fallback. Returns abs path.

    Cache-skips when hash(prompt) matches the sibling `.hash` file. Raises
    `VisualGenerationError` if both providers fail OR if moderation rejects.
    """
    filepaths.ensure_job_dirs(job_id)
    _moderate(image_prompt)

    artifact_path = filepaths.scene_image_path(job_id, scene_id)
    hash_path = filepaths.scene_image_hash_path(job_id, scene_id)
    expected = cache.hash_text(image_prompt)

    if cache.artifact_fresh(artifact_path, hash_path, expected):
        print(f"{_LOG_PREFIX} cache hit {artifact_path}")
        return artifact_path

    attempts: list[str] = []

    # --- Primary: Pollinations ---
    if quota.is_pollinations_unavailable(job_id):
        attempts.append("pollinations: skipped (marked unavailable earlier)")
    else:
        try:
            data = _via_pollinations(image_prompt)
            _write_png_safe(data, artifact_path)
            cache.write_hash_file(hash_path, expected)
            print(f"{_LOG_PREFIX} wrote {artifact_path} via pollinations ({len(data)} bytes)")
            return artifact_path
        except VisualGenerationError as exc:
            quota.mark_pollinations_unavailable(job_id)
            attempts.append(f"pollinations: {exc}")

    # --- Fallback: Imagen ---
    if quota.is_imagen_unavailable(job_id):
        attempts.append("imagen: skipped (marked unavailable earlier)")
    else:
        try:
            data = _via_imagen(image_prompt, job_id)
            _write_png_safe(data, artifact_path)
            cache.write_hash_file(hash_path, expected)
            print(f"{_LOG_PREFIX} wrote {artifact_path} via imagen ({len(data)} bytes)")
            return artifact_path
        except VisualGenerationError as exc:
            attempts.append(f"imagen: {exc}")

    summary = " | ".join(attempts) if attempts else "no providers attempted"
    raise VisualGenerationError(
        f"all visual providers failed for scene {scene_id}: {summary}"
    )


def generate_all_scene_images(script: Script) -> list[Path]:
    """Generate every scene image for `script` sequentially, in scene order."""
    paths: list[Path] = []
    for scene in script.scenes:
        paths.append(generate_scene_image(scene.image_prompt, script.job_id, scene.id))
    return paths
