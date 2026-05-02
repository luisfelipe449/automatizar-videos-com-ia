# Hook Video Mode — Design Spec

**Date:** 2026-05-02
**Status:** Approved
**Scope:** Add optional flag that makes only the FIRST scene of a Short be a video clip (via fal.ai), while scenes 2..N remain images. Complementary to the existing `USE_VIDEO` full-video mode.

## Motivation

The first ~2-3 seconds of a YouTube Short determine retention. A dynamic opening (motion video) outperforms a static image for "hook" scenes. Generating video for every scene via fal.ai is expensive and slow (10-230s per clip); the hook-only mode keeps costs bounded (1 clip per Short) while capturing most of the retention benefit.

## Scope (In)

- New env flag `USE_VIDEO_HOOK=true|false` (default: false).
- New function `generate_hook_clip` in `app/services/video_service.py`.
- New orchestrator function `generate_hook_plus_images` (scene 1 = video, rest = images).
- Bifurcation in `app/orchestrator.py::_stage_clips` to pick the right flow.
- Fallback to Pollinations image when fal.ai fails or credits run out.
- Prompt augmentation for the hook scene (append fixed suffix for motion/quality).
- Documentation in `.env.example` and `CLAUDE.md`.

## Scope (Out)

- Per-scene video toggle (only scene N is video) — future work.
- Dynamic fal.ai model selection based on cost tiers — out of scope.
- LLM generating a dedicated `hook_video_prompt` field — considered and rejected for MVP (see "Alternatives").
- Trimming/processing beyond what `render_service` already does.

## Interaction with `USE_VIDEO`

Flags are independent but `USE_VIDEO` takes precedence:

| `USE_VIDEO` | `USE_VIDEO_HOOK` | Behavior |
|---|---|---|
| true | any | All scenes as video (existing behavior). |
| false | true | Scene 1 as video, scenes 2..N as images. **NEW.** |
| false | false | All scenes as images (existing behavior, default). |

Documented in `.env.example`.

## Architecture

### Changes by file

**`app/config.py`** — add:
```python
USE_VIDEO_HOOK: bool = os.getenv("USE_VIDEO_HOOK", "false").strip().lower() in ("true", "1", "yes")
```

**`app/orchestrator.py::_stage_clips`** — new bifurcation:
```python
def _stage_clips(script: Script) -> list[Path]:
    from app import config
    if config.USE_VIDEO:
        from app.services.video_service import generate_all_scene_clips
        return generate_all_scene_clips(script)
    if config.USE_VIDEO_HOOK:
        from app.services.video_service import generate_hook_plus_images
        return generate_hook_plus_images(script)
    return generate_all_scene_images(script)
```

**`app/services/video_service.py`** — add two public functions:

1. `generate_hook_clip(script: Script) -> Path`:
   - Takes `script.scenes[0]`.
   - Builds prompt: `scene.image_prompt + " a highly detailed animation video with cinematic lighting, Unreal Engine 5, and depth of field."`
   - Calls `_attempt_fal(augmented_prompt, script.job_id, artifact_path)` (reuses existing helper — handles quota detection, download, validation, cache).
   - On `VideoGenerationError`, falls back to `visual_service.generate_scene_image(scene.image_prompt, script.job_id, scene.id)` — cena 1 becomes PNG.
   - Cache: hash computed from **raw `scene.image_prompt`** (not augmented), so `_clips_fresh` in orchestrator works unchanged. Suffix is deterministic; if we ever change it, manual cache cleanup is required (consistent with project convention).

2. `generate_hook_plus_images(script: Script) -> list[Path]`:
   - Scene 1 → `generate_hook_clip(script)`.
   - Scenes 2..N → iterate calling `visual_service.generate_scene_image(scene.image_prompt, script.job_id, scene.id)` directly.
   - Returns paths in scene order. Shape may be `[.mp4, .png, .png, ...]` (hook succeeded) or `[.png, .png, .png, ...]` (hook fell back).
   - No concurrency (only one fal.ai call; `visual_service.generate_scene_image` is fast enough to loop sequentially). Matches existing behavior of `generate_all_scene_images`, which is also sequential.

**`render_service`, `_clips_fresh`** — **no changes required**. Both already handle mixed `.mp4 + .png` scene paths by file extension.

**`.env.example`** — document new flag with ordering semantics.

**`CLAUDE.md`** — add brief mention under "Environment Variables" and "Pipeline Stages".

### Data flow (happy path)

```
prompt → script (5 scenes) → tts → captions →
  clips:
    scene 1: image_prompt + suffix → fal.ai ltx-video → scene_01.mp4 (5s)
    scene 2: image_prompt → Pollinations → scene_02.png
    scene 3: image_prompt → Pollinations → scene_03.png
    ...
  → render:
    scene 1: scale+crop+trim to scene.duration_seconds (≤5s)
    scenes 2..N: zoompan Ken Burns
    → final.mp4
  → upload
```

### Data flow (fal.ai credit exhausted)

```
clips:
  scene 1: image_prompt + suffix → fal.ai returns 429/payment →
           _is_quota_error detects →
           quota.mark_video_unavailable(job_id) →
           visual_service.generate_scene_image → scene_01.png
  scenes 2..N: Pollinations normal
render: all-image path (Ken Burns everywhere).
```

## Error Handling

Reuses existing infrastructure entirely:

| Failure | Handler | Result |
|---|---|---|
| fal.ai credit / quota / 429 | `_is_quota_error` in `_attempt_fal` | Marks video unavailable for job, falls back to Pollinations PNG. |
| `FAL_KEY` missing | `_attempt_fal` raises immediately (no retry) | Falls back to Pollinations PNG. |
| fal.ai network / 5xx | `default_retry` decorator on `_call_fal` | 3 attempts with exponential backoff; ultimate failure falls back to PNG. |
| Malformed fal.ai response | `_extract_video_url` raises `VideoGenerationError` | Falls back to PNG. |
| Downloaded MP4 invalid | `_validate_mp4` deletes + raises | Falls back to PNG. |
| Moderation rejects prompt | `visual_service._moderate` | Raises `VisualGenerationError` (hard fail — neither path works). |

No new error classes. No new quota flags. No new retry logic.

## Cache Semantics

- Artifact: `assets/generated/<job_id>/scene_01.mp4`
- Hash file: `assets/generated/<job_id>/scene_01.video.hash`
- Hash content: `cache.hash_text(scene.image_prompt)` — the RAW prompt (not augmented)

Rationale: `_clips_fresh` in orchestrator already computes `cache.hash_text(scene.image_prompt)` to decide freshness. Writing the augmented hash would require a parallel check path. Suffix is a compile-time constant, so raw-prompt hashing is safe: the same raw prompt always maps to the same augmented prompt.

Cross-mode cache behavior:

- Existing `scene_01.png` from a prior image-mode run is IGNORED by hook mode. New `scene_01.mp4` is generated. Acceptable (disk cost is trivial).
- Existing `scene_01.mp4` from a prior hook-mode run is REUSED (cache hit).
- `USE_VIDEO=true` → hook mode → scene 1 already has `.mp4`: reused as-is. Scenes 2..N regenerate as PNG (that's a mode transition; expected).

## Testing

### `tests/test_video_service.py` (new tests)

1. `test_generate_hook_clip_happy_path` — mock `fal_client.subscribe`, assert MP4 written, hash file written with raw prompt hash.
2. `test_generate_hook_clip_falls_back_on_quota` — mock fal.ai to raise with "payment" in message, assert PNG returned via `visual_service.generate_scene_image`.
3. `test_generate_hook_clip_falls_back_on_missing_key` — unset `FAL_KEY`, assert immediate PNG fallback.
4. `test_generate_hook_clip_cache_hit` — pre-seed MP4 + hash, assert no fal.ai call.
5. `test_generate_hook_plus_images_returns_mixed` — mock hook success + 2 image scenes, assert returned list is `[.mp4, .png, .png]`.

### `tests/test_orchestrator.py` (new test)

6. `test_stage_clips_uses_hook_flow_when_configured` — monkeypatch `config.USE_VIDEO=False, USE_VIDEO_HOOK=True`, mock `generate_hook_plus_images`, assert it's called (and `generate_all_scene_images` / `generate_all_scene_clips` are NOT).

### No new render tests

`render_service` handles mixed `.mp4 + .png` scene paths; existing tests for `USE_VIDEO=true` already exercise this.

## Alternatives Considered

**A) LLM-generated `hook_video_prompt` field in Script schema.** Rejected for MVP — adds a required/optional field to Pydantic contract, requires prompt engineering changes in `script_service`, and a re-prompt on validation failures. Fixed-suffix approach gets 80% of the quality at 5% of the effort. Can migrate later if motion quality is poor.

**B) Unify `USE_VIDEO` into `VIDEO_MODE=none|hook|all`.** Rejected — breaking change for users with existing `.env` files. Independent flags with documented precedence achieve the same outcome without migration pain.

**C) Accept whatever duration fal.ai returns (no trim).** Rejected — render_service already trims video scenes to `scene.duration_seconds` for `USE_VIDEO=true`. Reusing that logic keeps behavior uniform and fidelity to the script.

## Open Questions

None.

## Migration / Rollout

- Default `USE_VIDEO_HOOK=false` → zero behavior change for existing users.
- Feature flag flip is reversible: set to `false`, next run falls back to full-image mode.
- No database migration. No schema changes.
