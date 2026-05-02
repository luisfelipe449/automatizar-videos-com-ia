"""fal.ai LTX Video → scene_XX.mp4 (optional per-scene video mode).

Single responsibility: given an image prompt + job_id + scene_id, produce a
5s MP4 clip at `filepaths.scene_video_path(...)` via the fal.ai `ltx-video`
endpoint. Honors a hash-based cache so reruns are a no-op.

Fallback: on ANY `VideoGenerationError` (API failure, quota, invalid MP4,
moderation rejection) the service falls back to `visual_service.
generate_scene_image`, returning the PNG path instead. Callers are expected
to check the suffix of the returned path — render_service branches on it
per-scene.

Concurrency: `generate_all_scene_clips` uses a ThreadPoolExecutor
(max_workers=4) because fal.ai latency is 10-230s per clip and sequential
generation of 8 scenes would exceed practical wait times. Cache-fresh scenes
short-circuit without consuming a worker slot.

This module NEVER duplicates visual_service's blocklist: the moderation
predicate is reused by importing `_moderate`.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import fal_client
import requests

from app.models import Script
from app.services import visual_service
from app.services.visual_service import VisualGenerationError
from app.utils import cache, filepaths, quota
from app.utils.ffmpeg_helpers import FFmpegError, probe_duration
from app.utils.retries import default_retry

_LOG_PREFIX = "[video_service]"

# fal.ai endpoint for LTX Video. `aspect_ratio` / `resolution` params are
# IGNORED by the model today — output is always 768×512 horizontal 24fps.
_FAL_ENDPOINT = "fal-ai/ltx-video"

# Download timeout for the generated MP4 URL. The file is ~6MB so 120s is
# generous even on slow links.
_DOWNLOAD_TIMEOUT_S = 120

# Max workers for concurrent clip generation. fal.ai free tier tolerates
# this; sequential generation of 8 scenes takes 20+ minutes worst-case.
_MAX_WORKERS = 4

# Appended to scene 1's image_prompt when USE_VIDEO_HOOK=true to bias fal.ai
# LTX Video toward cinematic motion. Constant so cache keys (hashed from the
# RAW image_prompt) remain stable across runs — see
# docs/superpowers/specs/2026-05-02-hook-video-design.md for rationale.
_HOOK_PROMPT_SUFFIX = (
    " a highly detailed animation video with cinematic lighting, "
    "Unreal Engine 5, and depth of field."
)


class VideoGenerationError(RuntimeError):
    """Raised when fal.ai fails — BEFORE the visual_service fallback kicks in.

    Callers inside this module convert this into a call to
    `visual_service.generate_scene_image`. Any fallback failure surfaces as
    `VisualGenerationError` from that module.
    """


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

@default_retry
def _call_fal(prompt: str) -> dict:
    """Submit one prompt to fal.ai LTX Video. Returns the raw result dict.

    Wrapped in `default_retry` so transient 5xx / timeout errors back off and
    retry up to 3 times. Non-transient failures (e.g. invalid prompt,
    authentication) raise immediately.
    """
    # fal_client reads FAL_KEY from the environment on every call, so we
    # don't have to thread it through.
    return fal_client.subscribe(_FAL_ENDPOINT, arguments={"prompt": prompt})


def _extract_video_url(result: dict) -> str:
    """Return the MP4 URL from a fal.ai response. Raises on malformed shape."""
    video = result.get("video") if isinstance(result, dict) else None
    if not isinstance(video, dict):
        raise VideoGenerationError(
            f"fal.ai response missing 'video' object: {result!r}"
        )
    url = video.get("url")
    if not isinstance(url, str) or not url:
        raise VideoGenerationError(
            f"fal.ai response missing 'video.url': {result!r}"
        )
    return url


def _download_video(url: str, out_path: Path) -> None:
    """Stream the MP4 to `out_path` via an atomic `.part` rename.

    Atomic rename prevents a crashed/partial download from fooling the cache
    into thinking the artifact is fresh. Raises `VideoGenerationError` on
    any network/IO failure.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".part")
    try:
        response = requests.get(url, timeout=_DOWNLOAD_TIMEOUT_S, stream=True)
        response.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if chunk:
                    fh.write(chunk)
    except requests.RequestException as exc:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise VideoGenerationError(f"failed to download fal.ai clip: {exc}") from exc

    try:
        tmp.replace(out_path)
    except OSError as exc:
        raise VideoGenerationError(f"failed to finalize clip {out_path}: {exc}") from exc


def _validate_mp4(path: Path) -> None:
    """Confirm `path` decodes with a non-zero duration. Deletes and raises on failure."""
    try:
        duration = probe_duration(path)
    except (FFmpegError, FileNotFoundError, ValueError) as exc:
        _unlink_quiet(path)
        raise VideoGenerationError(f"ffprobe rejected {path.name}: {exc}") from exc
    if duration <= 0:
        _unlink_quiet(path)
        raise VideoGenerationError(
            f"ffprobe reports {path.name} duration={duration:.3f}s (<=0)"
        )


def _unlink_quiet(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def _is_quota_error(message: str) -> bool:
    """Heuristic: decide if a fal.ai error string indicates quota exhaustion."""
    lowered = message.lower()
    return any(
        token in lowered
        for token in ("quota", "rate limit", "rate_limit", "429", "payment", "billing")
    )


def _attempt_fal(image_prompt: str, job_id: str, artifact_path: Path) -> None:
    """Try once to produce `artifact_path` via fal.ai. Raises VideoGenerationError."""
    if quota.is_video_unavailable(job_id):
        raise VideoGenerationError(
            "fal.ai video marked unavailable earlier this run"
        )
    if not os.getenv("FAL_KEY", "").strip():
        # Don't burn a retry on a missing credential; surface fast.
        quota.mark_video_unavailable(job_id)
        raise VideoGenerationError("FAL_KEY is not set; cannot reach fal.ai")

    try:
        result = _call_fal(image_prompt)
    except VideoGenerationError:
        raise
    except Exception as exc:  # noqa: BLE001 — quota vs generic handled below
        if _is_quota_error(str(exc)):
            quota.mark_video_unavailable(job_id)
            raise VideoGenerationError(f"fal.ai quota/rate-limit: {exc}") from exc
        raise VideoGenerationError(f"fal.ai call failed: {exc}") from exc

    url = _extract_video_url(result)
    _download_video(url, artifact_path)
    _validate_mp4(artifact_path)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def generate_scene_clip(image_prompt: str, job_id: str, scene_id: int) -> Path:
    """Produce `scene_XX.mp4` via fal.ai LTX Video. Returns absolute path.

    Cache-skips when hash(image_prompt) matches the sibling `.video.hash`.
    On ANY fal.ai failure (network, quota, moderation, malformed payload,
    invalid MP4) falls back to `visual_service.generate_scene_image` — the
    returned path in that case is `.png`. Callers MUST check the suffix.
    """
    filepaths.ensure_job_dirs(job_id)

    # Reuse visual_service's local blocklist so prompts rejected there are
    # rejected here too. This raises VisualGenerationError; we propagate it
    # unchanged so the caller sees the same error class they'd see for an
    # image-only pipeline.
    visual_service._moderate(image_prompt)

    artifact_path = filepaths.scene_video_path(job_id, scene_id)
    hash_path = filepaths.scene_video_hash_path(job_id, scene_id)
    expected = cache.hash_text(image_prompt)

    if cache.artifact_fresh(artifact_path, hash_path, expected):
        print(f"{_LOG_PREFIX} cache hit {artifact_path}")
        return artifact_path

    try:
        _attempt_fal(image_prompt, job_id, artifact_path)
    except VideoGenerationError as exc:
        # Clean up any half-written file before fallback takes over.
        _unlink_quiet(artifact_path)
        print(
            f"{_LOG_PREFIX} scene {scene_id} failed; falling back to image: {exc}"
        )
        # Fallback: a PNG via visual_service. Caller checks the suffix.
        return visual_service.generate_scene_image(image_prompt, job_id, scene_id)

    cache.write_hash_file(hash_path, expected)
    print(f"{_LOG_PREFIX} wrote {artifact_path} via fal.ai")
    return artifact_path


def generate_all_scene_clips(script: Script) -> list[Path]:
    """Generate every scene clip in parallel (max 4 workers), preserving order.

    Returns paths in scene order. Each path may be `.mp4` (fal.ai success
    or cache hit) or `.png` (fallback to image). Cache-fresh scenes do not
    consume a worker slot — they resolve inline before the pool runs.
    """
    # Phase 1: inline cache hits so the thread pool only does real work.
    # Pre-compute results by scene index. Unknown entries are produced by
    # the executor in phase 2.
    results: list[Path | None] = [None] * len(script.scenes)
    pending: list[tuple[int, int, str]] = []  # (index, scene_id, prompt)

    for idx, scene in enumerate(script.scenes):
        # Moderation & cache-freshness check: doing this upfront avoids
        # spawning a thread for something the main thread can resolve.
        try:
            visual_service._moderate(scene.image_prompt)
        except VisualGenerationError:
            # Let the worker path raise uniformly — don't swallow here.
            pending.append((idx, scene.id, scene.image_prompt))
            continue

        artifact_path = filepaths.scene_video_path(script.job_id, scene.id)
        hash_path = filepaths.scene_video_hash_path(script.job_id, scene.id)
        expected = cache.hash_text(scene.image_prompt)
        if cache.artifact_fresh(artifact_path, hash_path, expected):
            print(f"{_LOG_PREFIX} cache hit {artifact_path}")
            results[idx] = artifact_path
            continue

        # Also accept a cached IMAGE fallback from a previous run. We check
        # the image cache so we don't accidentally regenerate a clip when
        # fal.ai failed last time and left a valid PNG behind.
        image_path = filepaths.scene_image_path(script.job_id, scene.id)
        image_hash_path = filepaths.scene_image_hash_path(script.job_id, scene.id)
        if cache.artifact_fresh(image_path, image_hash_path, expected) and not artifact_path.exists():
            # Only honour the image cache if there's no video to trump it.
            # Not a short-circuit — we still TRY fal.ai. Putting this check
            # in pending keeps the control flow simple.
            pass

        pending.append((idx, scene.id, scene.image_prompt))

    if not pending:
        # Everything cache-hit. `results` is fully populated.
        return [p for p in results if p is not None]

    # Phase 2: concurrent generation for everything not cache-fresh.
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {
            pool.submit(generate_scene_clip, prompt, script.job_id, scene_id): idx
            for (idx, scene_id, prompt) in pending
        }
        for future in as_completed(futures):
            idx = futures[future]
            # Propagate the exception. ThreadPoolExecutor.__exit__ will cancel
            # any un-started futures but in-flight ones still run to
            # completion — acceptable for a small (<=8) fan-out.
            results[idx] = future.result()

    return [p for p in results if p is not None]
