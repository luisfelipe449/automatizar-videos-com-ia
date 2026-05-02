"""End-to-end pipeline orchestrator (Phase 9 of the pipeline).

Single responsibility: run all 6 pipeline stages (script → tts → captions →
clips → render → upload) for a given user prompt. Idempotent — each stage
short-circuits when its artifact is already present and fresh. Writes a
`status.json` after every stage, so a crashed run can be inspected and resumed.

The `clips` stage generates either per-scene PNGs (default, MVP) or per-scene
MP4s (when `USE_VIDEO=true`, via fal.ai LTX Video). On any per-scene fal.ai
failure, video_service transparently falls back to a PNG — render_service
branches on the `.mp4`/`.png` suffix to pick the right filter chain per scene.

No business logic lives here — the stages delegate to `app/services/`. This
module is just flow control + cache checks + status.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TypeVar

from app.models import RenderSpec, Script
from app.services.caption_service import transcribe
from app.services.render_service import render_short
from app.services.script_service import generate_script
from app.services.tts_service import synthesize_narration
from app.services.visual_service import generate_all_scene_images
from app.services.youtube_service import upload_short
from app.utils import cache, filepaths

_LOG_PREFIX = "[orchestrator]"
# Stage name history: `visuals` was renamed to `clips` when video-per-scene
# mode landed. Existing status.json files from older runs become stale —
# they're disposable cache and will be regenerated on the next run.
_STAGE_NAMES = ("script", "tts", "captions", "clips", "render", "upload")
T = TypeVar("T")


@dataclass
class StageResult:
    """Uniform per-stage record persisted inside `status.json`."""

    status: str = "pending"  # pending|cache-hit|running|done|failed|skipped
    elapsed_s: float = 0.0
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "elapsed_s": round(self.elapsed_s, 3),
            "error": self.error,
        }
        payload.update(self.extra)  # flatten e.g. {"video_id": "..."}
        return payload


class _StageFailure(Exception):
    """Internal wrapper so `run_pipeline` can stamp status before re-raising."""

    def __init__(self, stage: str, original: BaseException, elapsed: float):
        super().__init__(str(original))
        self.stage, self.original, self.elapsed = stage, original, elapsed


def _log(stage: str, status: str, elapsed: float) -> None:
    print(f"{_LOG_PREFIX} stage={stage} status={status} elapsed={elapsed:.2f}s")


def _write_status(
    job_id: str, prompt: str, stages: dict[str, StageResult], *, ok: bool
) -> None:
    """Persist the full status document to `status.json`."""
    filepaths.ensure_job_dirs(job_id)
    doc = {
        "job_id": job_id,
        "prompt": prompt,
        "stages": {name: stages[name].to_dict() for name in _STAGE_NAMES},
        "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "ok": ok,
    }
    filepaths.status_path(job_id).write_text(
        json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _run_stage(
    name: str,
    fn: Callable[[], T],
    *,
    check_cache: Callable[[], T | None] | None = None,
) -> tuple[T, StageResult]:
    """Execute one stage. `check_cache` non-None → treat as cache-hit.

    On failure, wraps the original exception in `_StageFailure` (which carries
    the elapsed time) so `run_pipeline` can persist status before re-raising.
    """
    start = time.perf_counter()
    if check_cache is not None:
        cached = check_cache()
        if cached is not None:
            elapsed = time.perf_counter() - start
            _log(name, "cache-hit", elapsed)
            return cached, StageResult(status="cache-hit", elapsed_s=elapsed)

    _log(name, "running", 0.0)
    try:
        value = fn()
    except Exception as exc:
        elapsed = time.perf_counter() - start
        _log(name, "failed", elapsed)
        raise _StageFailure(name, exc, elapsed) from exc

    elapsed = time.perf_counter() - start
    _log(name, "done", elapsed)
    return value, StageResult(status="done", elapsed_s=elapsed)


# --------------------------------------------------------------------------- #
# Stage helpers — thin wrappers + cache predicates. No business logic.
# --------------------------------------------------------------------------- #

def _load_cached_script(job_id: str) -> Script | None:
    path = filepaths.script_path(job_id)
    if not path.exists():
        return None
    try:
        return Script.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:
        # Corrupted script.json — force regeneration rather than crashing.
        return None


def _stage_script(prompt: str, job_id: str) -> Script:
    return generate_script(prompt, job_id=job_id)


def _stage_tts(script: Script) -> Path:
    return synthesize_narration(script.narration, script.job_id)


def _stage_captions(script: Script) -> Path:
    return transcribe(
        filepaths.narration_path(script.job_id),
        script.job_id,
        narration_hash=cache.hash_text(script.narration),
    )


def _stage_clips(script: Script) -> list[Path]:
    """Generate all scene clips.

    Branches on config flags:
    - `USE_VIDEO=true`: every scene is a video (via fal.ai, parallel).
    - `USE_VIDEO_HOOK=true` (and USE_VIDEO=false): scene 1 is a video,
      scenes 2..N are images.
    - Otherwise: every scene is an image (MVP default).

    USE_VIDEO takes precedence when both flags are set.
    """
    from app import config

    if config.USE_VIDEO:
        from app.services.video_service import generate_all_scene_clips
        return generate_all_scene_clips(script)
    if config.USE_VIDEO_HOOK:
        from app.services.video_service import generate_hook_plus_images
        return generate_hook_plus_images(script)
    return generate_all_scene_images(script)


def _stage_render(
    script: Script, scene_paths: list[Path], narration: Path, captions: Path
) -> Path:
    return render_short(RenderSpec(
        job_id=script.job_id,
        scenes=script.scenes,
        scene_paths=scene_paths,
        narration_path=narration,
        captions_path=captions,
        output_path=filepaths.final_video_path(script.job_id),
    ))


def _stage_upload(script: Script, video_path: Path, category: int | None = None) -> str:
    return upload_short(video_path, script, category=category)


def _tts_fresh(script: Script) -> Path | None:
    wav = filepaths.narration_path(script.job_id)
    hsh = filepaths.narration_hash_path(script.job_id)
    return wav if cache.artifact_fresh(wav, hsh, cache.hash_text(script.narration)) else None


def _captions_fresh(script: Script) -> Path | None:
    """Captions are fresh iff the sibling hash matches `hash_text(narration)`.

    Protects against stale captions left behind from a previous narration: if
    the narration text changes, tts_service regenerates the WAV and the
    captions' hash mismatches, forcing a retranscribe.
    """
    cp = filepaths.captions_path(script.job_id)
    hp = filepaths.captions_hash_path(script.job_id)
    expected = cache.hash_text(script.narration)
    return cp if cache.artifact_fresh(cp, hp, expected) else None


def _clips_fresh(script: Script) -> list[Path] | None:
    """Return paths iff EVERY scene artifact is cache-fresh.

    Accepts either `.mp4` (video-per-scene mode) or `.png` (image mode or
    fallback from a prior video-mode run) — whichever the current job's
    cache has on disk. Configured provider is NOT re-checked; if a mixed
    set of artifacts already satisfies every scene we use them as-is.
    """
    paths: list[Path] = []
    for scene in script.scenes:
        expected = cache.hash_text(scene.image_prompt)
        video_art = filepaths.scene_video_path(script.job_id, scene.id)
        video_hash = filepaths.scene_video_hash_path(script.job_id, scene.id)
        image_art = filepaths.scene_image_path(script.job_id, scene.id)
        image_hash = filepaths.scene_image_hash_path(script.job_id, scene.id)

        # Prefer the video artifact when both exist — it's higher fidelity
        # for USE_VIDEO runs. Falls through to the image path on PNG-only
        # jobs or when a prior fal.ai attempt fell back to an image.
        if cache.artifact_fresh(video_art, video_hash, expected):
            paths.append(video_art)
        elif cache.artifact_fresh(image_art, image_hash, expected):
            paths.append(image_art)
        else:
            return None
    return paths


def _render_fresh(
    script: Script, scene_paths: list[Path], narration: Path, captions: Path
) -> Path | None:
    """final.mp4 is fresh iff it exists AND its mtime >= every input's mtime."""
    final = filepaths.final_video_path(script.job_id)
    if not final.exists():
        return None
    try:
        final_mtime = final.stat().st_mtime
        for inp in (narration, captions, *scene_paths):
            if inp.stat().st_mtime > final_mtime:
                return None
    except OSError:
        return None
    return final


def _upload_fresh(job_id: str) -> str | None:
    rec = filepaths.upload_record_path(job_id)
    if not rec.exists():
        return None
    try:
        vid = json.loads(rec.read_text(encoding="utf-8")).get("video_id")
    except (json.JSONDecodeError, OSError):
        return None
    if vid:
        print(f"{_LOG_PREFIX} upload cache hit video_id={vid}")
        return str(vid)
    return None


# --------------------------------------------------------------------------- #
# Result formatting
# --------------------------------------------------------------------------- #

def _format_run_result(
    stages: dict[str, StageResult], skip_upload: bool
) -> dict[str, Any]:
    """Format pipeline execution result into the public API format.

    Returns a dict with:
    - success (bool): True if all stages completed without error
    - video_id (str|None): YouTube video ID if uploaded, None otherwise
    - stages_completed (list[str]): Stages with status "done" or "cache-hit"
    - error (str|None): Error message from first failed stage, or None
    """
    # Collect completed stages (successful runs, not pending/failed/skipped)
    stages_completed = [
        name for name in _STAGE_NAMES
        if stages[name].status in ("done", "cache-hit")
    ]

    # If upload was skipped, count it as completed
    if skip_upload and stages["upload"].status == "skipped":
        stages_completed.append("upload")

    # Find first error
    error: str | None = None
    for stage in _STAGE_NAMES:
        if stages[stage].status == "failed":
            error = stages[stage].error
            break

    # Extract video_id from upload stage if present
    video_id: str | None = stages["upload"].extra.get("video_id")

    # Success means all stages were done/cache-hit and no errors
    success = error is None and len(stages_completed) == len(_STAGE_NAMES)

    return {
        "success": success,
        "video_id": video_id,
        "stages_completed": stages_completed,
        "error": error,
    }


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def run_pipeline(
    prompt: str,
    *,
    job_id: str | None = None,
    skip_upload: bool = False,
    account: dict[str, Any] | None = None,
    category: int | None = None,
) -> dict[str, Any]:
    """Run the full pipeline. Returns a dict with execution results.

    Args:
        prompt: The user prompt for content generation.
        job_id: Optional override (otherwise derived from `prompt`).
        skip_upload: If True, stops after render (useful for local testing).
        account: Optional account dict (for multi-account support). Currently unused.
        category: YouTube category ID (default: 24 = Entertainment).

    Returns:
        A dict with keys:
        - success (bool): True if all stages completed without error
        - video_id (str|None): YouTube video ID if uploaded, None otherwise
        - stages_completed (list[str]): Names of stages that ran successfully
        - error (str|None): Error message from first failure, None if success=True
    """
    # Suppress unused parameter warning for future use
    _ = account

    jid = job_id or filepaths.make_job_id(prompt)
    filepaths.ensure_job_dirs(jid)
    stages: dict[str, StageResult] = {name: StageResult() for name in _STAGE_NAMES}
    flush = lambda ok: _write_status(jid, prompt, stages, ok=ok)  # noqa: E731

    try:
        script, stages["script"] = _run_stage(
            "script",
            lambda: _stage_script(prompt, jid),
            check_cache=lambda: _load_cached_script(jid),
        )
        flush(False)

        narration_path, stages["tts"] = _run_stage(
            "tts", lambda: _stage_tts(script), check_cache=lambda: _tts_fresh(script)
        )
        flush(False)

        captions_path, stages["captions"] = _run_stage(
            "captions", lambda: _stage_captions(script),
            check_cache=lambda: _captions_fresh(script),
        )
        flush(False)

        scene_paths, stages["clips"] = _run_stage(
            "clips", lambda: _stage_clips(script),
            check_cache=lambda: _clips_fresh(script),
        )
        flush(False)

        video_path, stages["render"] = _run_stage(
            "render",
            lambda: _stage_render(script, scene_paths, narration_path, captions_path),
            check_cache=lambda: _render_fresh(
                script, scene_paths, narration_path, captions_path
            ),
        )
        flush(False)

        if skip_upload:
            stages["upload"] = StageResult(status="skipped")
            _log("upload", "skipped", 0.0)
        else:
            video_id, result = _run_stage(
                "upload",
                lambda: _stage_upload(script, video_path, category),
                check_cache=lambda: _upload_fresh(jid),
            )
            result.extra["video_id"] = video_id
            stages["upload"] = result

        flush(True)
        return _format_run_result(stages, skip_upload)

    except _StageFailure as failure:
        stages[failure.stage] = StageResult(
            status="failed", elapsed_s=failure.elapsed, error=str(failure.original)
        )
        flush(False)
        raise failure.original from None


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.orchestrator",
        description="Generate and (optionally) upload a YouTube Short from a text prompt.",
    )
    parser.add_argument("--prompt", required=True, help="Tema do Short em linguagem natural.")
    parser.add_argument("--job-id", default=None, help="Override the derived job_id.")
    parser.add_argument("--skip-upload", action="store_true", help="Render only, do not upload.")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        result = run_pipeline(args.prompt, job_id=args.job_id, skip_upload=args.skip_upload)
        # CLI backward compatibility: exit 0 on success, 1 on error, 2 on exception
        return 0 if result["success"] else 1
    except Exception as exc:
        print(f"{_LOG_PREFIX} pipeline failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
