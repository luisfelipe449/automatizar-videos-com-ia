# Hook Video Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add optional `USE_VIDEO_HOOK` flag that renders only scene 1 as a video (fal.ai LTX Video) while scenes 2..N remain images, with automatic fallback to Pollinations image when fal.ai credits run out.

**Architecture:** One new env flag in `config.py`, one new bifurcation in `orchestrator._stage_clips`, two new functions in `video_service.py` (`generate_hook_clip`, `generate_hook_plus_images`). Reuses existing `_attempt_fal`, `_is_quota_error`, and `visual_service.generate_scene_image` for the fallback path. No changes to `render_service` (already handles mixed `.mp4` + `.png` scene paths). No schema/DB changes.

**Tech Stack:** Python 3.13, fal.ai via `fal_client`, pytest with monkeypatch.

**Spec:** `docs/superpowers/specs/2026-05-02-hook-video-design.md`

---

## File Structure

- **Modify** `app/config.py` — add `USE_VIDEO_HOOK` constant.
- **Modify** `app/services/video_service.py` — add `_HOOK_PROMPT_SUFFIX`, `generate_hook_clip`, `generate_hook_plus_images`.
- **Modify** `app/orchestrator.py::_stage_clips` — new bifurcation for hook mode.
- **Modify** `tests/test_video_service.py` — 5 new tests.
- **Modify** `tests/test_orchestrator.py` — 1 new test.
- **Modify** `.env.example` — document new flag.
- **Modify** `CLAUDE.md` — brief mention in Environment Variables section.

---

## Task 1: Add `USE_VIDEO_HOOK` config flag

**Files:**
- Modify: `app/config.py`

- [ ] **Step 1: Add the flag**

In `app/config.py`, immediately after the existing `USE_VIDEO` block (around line 59), add:

```python
# -----------------------------------------------------------------------------
# Optional hook-only video mode.
# -----------------------------------------------------------------------------
# When USE_VIDEO_HOOK is truthy AND USE_VIDEO is falsy, the visuals stage
# generates a single MP4 for scene 1 (via fal.ai LTX Video) while scenes
# 2..N stay as PNGs. Captures the "hook" retention boost of motion without
# paying for a full-video Short. Ignored when USE_VIDEO=true (USE_VIDEO
# wins — all scenes become video).
USE_VIDEO_HOOK: bool = os.getenv("USE_VIDEO_HOOK", "false").strip().lower() in ("true", "1", "yes")
```

- [ ] **Step 2: Sanity check import**

Run: `python -c "from app import config; print(config.USE_VIDEO_HOOK)"`
Expected: `False` (no env var set)

- [ ] **Step 3: Commit**

```bash
git add app/config.py
git commit -m "feat(config): add USE_VIDEO_HOOK flag"
```

---

## Task 2: Add prompt suffix constant to video_service

**Files:**
- Modify: `app/services/video_service.py`

- [ ] **Step 1: Add the constant**

In `app/services/video_service.py`, immediately after the existing `_MAX_WORKERS = 4` line (around line 50), add:

```python
# Appended to scene 1's image_prompt when USE_VIDEO_HOOK=true to bias fal.ai
# LTX Video toward cinematic motion. Constant so cache keys (hashed from the
# RAW image_prompt) remain stable across runs — see
# docs/superpowers/specs/2026-05-02-hook-video-design.md for rationale.
_HOOK_PROMPT_SUFFIX = (
    " a highly detailed animation video with cinematic lighting, "
    "Unreal Engine 5, and depth of field."
)
```

- [ ] **Step 2: Commit**

```bash
git add app/services/video_service.py
git commit -m "feat(video): add hook prompt suffix constant"
```

---

## Task 3: Write failing test for `generate_hook_clip` happy path

**Files:**
- Test: `tests/test_video_service.py`

- [ ] **Step 1: Read existing fixtures**

Run: `grep -n "^def \|^class \|@pytest.fixture" tests/test_video_service.py | head -40`

Read lines 1-120 to understand `_FakeStreamResponse`, `_fake_fal_result`, `patched_env`, `no_backoff`, `_patch_fal_subscribe`.

- [ ] **Step 2: Add helper to build a Script fixture (if not already present)**

At the top of `tests/test_video_service.py`, after the existing imports, verify a `_make_script` helper exists. If NOT present, add:

```python
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
```

If a similar helper already exists, reuse it and skip this step.

- [ ] **Step 3: Add the failing test**

Append to `tests/test_video_service.py`:

```python
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
```

- [ ] **Step 4: Run the test to verify it fails**

Run: `pytest tests/test_video_service.py::test_generate_hook_clip_happy_path -v`
Expected: FAIL with `AttributeError: module 'app.services.video_service' has no attribute 'generate_hook_clip'`

- [ ] **Step 5: Commit**

```bash
git add tests/test_video_service.py
git commit -m "test(video): add failing happy-path test for generate_hook_clip"
```

---

## Task 4: Implement `generate_hook_clip` — happy path

**Files:**
- Modify: `app/services/video_service.py`

- [ ] **Step 1: Add the function**

At the end of `app/services/video_service.py`, append:

```python
def generate_hook_clip(script: Script) -> Path:
    """Produce `scene_01.mp4` via fal.ai using an AUGMENTED prompt.

    Used by the `USE_VIDEO_HOOK` pipeline mode: only scene 1 becomes a video;
    the rest stay as images. Prompt sent to fal.ai is
    `scene.image_prompt + _HOOK_PROMPT_SUFFIX`. Cache hash is computed from
    the RAW prompt (not augmented) so `orchestrator._clips_fresh` — which
    hashes raw prompts — remains compatible.

    On ANY fal.ai failure (including quota/credit exhaustion, missing
    FAL_KEY, moderation), transparently falls back to
    `visual_service.generate_scene_image`, returning a `.png` path.
    Callers MUST check the suffix of the returned path.
    """
    scene = script.scenes[0]
    filepaths.ensure_job_dirs(script.job_id)

    # Moderation runs over the RAW prompt; blocklist is about user content,
    # not our motion suffix.
    visual_service._moderate(scene.image_prompt)

    artifact_path = filepaths.scene_video_path(script.job_id, scene.id)
    hash_path = filepaths.scene_video_hash_path(script.job_id, scene.id)
    expected = cache.hash_text(scene.image_prompt)

    if cache.artifact_fresh(artifact_path, hash_path, expected):
        print(f"{_LOG_PREFIX} hook cache hit {artifact_path}")
        return artifact_path

    augmented = scene.image_prompt + _HOOK_PROMPT_SUFFIX

    try:
        _attempt_fal(augmented, script.job_id, artifact_path)
    except VideoGenerationError as exc:
        _unlink_quiet(artifact_path)
        print(
            f"{_LOG_PREFIX} hook scene failed; falling back to image: {exc}"
        )
        return visual_service.generate_scene_image(
            scene.image_prompt, script.job_id, scene.id
        )

    cache.write_hash_file(hash_path, expected)
    print(f"{_LOG_PREFIX} wrote hook {artifact_path} via fal.ai")
    return artifact_path
```

- [ ] **Step 2: Run the failing test to verify it passes**

Run: `pytest tests/test_video_service.py::test_generate_hook_clip_happy_path -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add app/services/video_service.py
git commit -m "feat(video): implement generate_hook_clip for USE_VIDEO_HOOK mode"
```

---

## Task 5: Write failing test for hook fallback on fal.ai credit exhaustion

**Files:**
- Test: `tests/test_video_service.py`

- [ ] **Step 1: Add the test**

Append to `tests/test_video_service.py`:

```python
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
```

- [ ] **Step 2: Run the test**

Run: `pytest tests/test_video_service.py::test_generate_hook_clip_falls_back_on_payment_error -v`
Expected: PASS (implementation from Task 4 already handles this path)

- [ ] **Step 3: Commit**

```bash
git add tests/test_video_service.py
git commit -m "test(video): cover hook fallback on fal.ai credit exhaustion"
```

---

## Task 6: Write failing test for hook fallback when `FAL_KEY` is missing

**Files:**
- Test: `tests/test_video_service.py`

- [ ] **Step 1: Add the test**

Append to `tests/test_video_service.py`:

```python
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
```

- [ ] **Step 2: Run the test**

Run: `pytest tests/test_video_service.py::test_generate_hook_clip_falls_back_when_fal_key_missing -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_video_service.py
git commit -m "test(video): cover hook fallback when FAL_KEY is missing"
```

---

## Task 7: Write failing test for hook cache hit

**Files:**
- Test: `tests/test_video_service.py`

- [ ] **Step 1: Add the test**

Append to `tests/test_video_service.py`:

```python
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
```

- [ ] **Step 2: Run the test**

Run: `pytest tests/test_video_service.py::test_generate_hook_clip_cache_hit -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_video_service.py
git commit -m "test(video): cover hook cache hit"
```

---

## Task 8: Write failing test for `generate_hook_plus_images`

**Files:**
- Test: `tests/test_video_service.py`

- [ ] **Step 1: Add the test**

Append to `tests/test_video_service.py`:

```python
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
```

- [ ] **Step 2: Run the test — verify it fails**

Run: `pytest tests/test_video_service.py::test_generate_hook_plus_images_returns_mixed_paths -v`
Expected: FAIL with `AttributeError: module 'app.services.video_service' has no attribute 'generate_hook_plus_images'`

- [ ] **Step 3: Commit**

```bash
git add tests/test_video_service.py
git commit -m "test(video): add failing test for generate_hook_plus_images"
```

---

## Task 9: Implement `generate_hook_plus_images`

**Files:**
- Modify: `app/services/video_service.py`

- [ ] **Step 1: Add the function**

At the end of `app/services/video_service.py`, append:

```python
def generate_hook_plus_images(script: Script) -> list[Path]:
    """Scene 1 as video (via `generate_hook_clip`), scenes 2..N as PNGs.

    Returns paths in scene order. Shape is `[.mp4, .png, .png, ...]` on the
    happy path or `[.png, .png, .png, ...]` if the hook fell back to image.
    Sequential (no ThreadPoolExecutor) — only one fal.ai call; image
    generation is I/O-bound but fast enough that added complexity is not
    justified.
    """
    paths: list[Path] = [generate_hook_clip(script)]
    for scene in script.scenes[1:]:
        paths.append(
            visual_service.generate_scene_image(
                scene.image_prompt, script.job_id, scene.id
            )
        )
    return paths
```

- [ ] **Step 2: Run the test to verify it passes**

Run: `pytest tests/test_video_service.py::test_generate_hook_plus_images_returns_mixed_paths -v`
Expected: PASS

- [ ] **Step 3: Run the full video_service test file**

Run: `pytest tests/test_video_service.py -v`
Expected: ALL PASS (existing + 5 new tests)

- [ ] **Step 4: Commit**

```bash
git add app/services/video_service.py
git commit -m "feat(video): implement generate_hook_plus_images orchestrator"
```

---

## Task 10: Write failing test for orchestrator bifurcation

**Files:**
- Test: `tests/test_orchestrator.py`

- [ ] **Step 1: Add the test**

Append to `tests/test_orchestrator.py`:

```python
def test_stage_clips_uses_hook_flow_when_configured(monkeypatch, redirected_dirs, tmp_path):
    """USE_VIDEO=false + USE_VIDEO_HOOK=true → hook flow, not all-image, not all-video."""
    from app import config, orchestrator
    from app.services import visual_service, video_service

    monkeypatch.setattr(config, "USE_VIDEO", False)
    monkeypatch.setattr(config, "USE_VIDEO_HOOK", True)

    called = {"hook": 0, "all_video": 0, "all_images": 0}

    def fake_hook(script):
        called["hook"] += 1
        return [tmp_path / "scene_01.mp4", tmp_path / "scene_02.png", tmp_path / "scene_03.png"]

    def fake_all_video(script):
        called["all_video"] += 1
        return []

    def fake_all_images(script):
        called["all_images"] += 1
        return []

    monkeypatch.setattr(
        "app.services.video_service.generate_hook_plus_images", fake_hook
    )
    monkeypatch.setattr(
        "app.services.video_service.generate_all_scene_clips", fake_all_video
    )
    monkeypatch.setattr(
        "app.orchestrator.generate_all_scene_images", fake_all_images
    )

    script = _make_script("orch-hook-test")
    result = orchestrator._stage_clips(script)

    assert called == {"hook": 1, "all_video": 0, "all_images": 0}
    assert len(result) == 3
```

- [ ] **Step 2: Run the test — verify it fails**

Run: `pytest tests/test_orchestrator.py::test_stage_clips_uses_hook_flow_when_configured -v`
Expected: FAIL (orchestrator currently only checks `USE_VIDEO`, hook branch missing → `generate_all_scene_images` is called instead)

- [ ] **Step 3: Commit**

```bash
git add tests/test_orchestrator.py
git commit -m "test(orchestrator): add failing test for hook-mode bifurcation"
```

---

## Task 11: Implement orchestrator bifurcation

**Files:**
- Modify: `app/orchestrator.py`

- [ ] **Step 1: Update `_stage_clips`**

Locate `_stage_clips` in `app/orchestrator.py` (around line 156). Replace the entire function body with:

```python
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
```

- [ ] **Step 2: Run the orchestrator test**

Run: `pytest tests/test_orchestrator.py::test_stage_clips_uses_hook_flow_when_configured -v`
Expected: PASS

- [ ] **Step 3: Run the full orchestrator suite**

Run: `pytest tests/test_orchestrator.py -v`
Expected: ALL PASS (no regressions)

- [ ] **Step 4: Commit**

```bash
git add app/orchestrator.py
git commit -m "feat(orchestrator): wire USE_VIDEO_HOOK into _stage_clips bifurcation"
```

---

## Task 12: Document the new flag in `.env.example`

**Files:**
- Modify: `.env.example`

- [ ] **Step 1: Find the existing `USE_VIDEO` entry**

Run: `grep -n "USE_VIDEO" .env.example`
Note the line number.

- [ ] **Step 2: Add `USE_VIDEO_HOOK` immediately below it**

Insert these lines directly after the existing `USE_VIDEO=...` line (and any comments that belong to `USE_VIDEO`):

```env
# USE_VIDEO_HOOK — optional. When set to true AND USE_VIDEO=false, only
# scene 1 is rendered as a video (via fal.ai LTX Video); scenes 2..N stay
# as images. Cheaper than USE_VIDEO=true (1 clip vs N), captures the
# retention boost of motion in the hook. Falls back to Pollinations image
# if fal.ai credits run out. Ignored when USE_VIDEO=true. Requires FAL_KEY.
USE_VIDEO_HOOK=false
```

- [ ] **Step 3: Commit**

```bash
git add .env.example
git commit -m "docs(env): document USE_VIDEO_HOOK flag"
```

---

## Task 13: Update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update the `Environment Variables` → `Optional` list**

Find the "Optional" list under "## Environment Variables". Find the line for `USE_VIDEO`:

```markdown
- `USE_VIDEO`: `true` to enable per-scene video clips via fal.ai LTX Video (requires `FAL_KEY`)
```

Add a new bullet directly below it:

```markdown
- `USE_VIDEO_HOOK`: `true` to make only scene 1 a video (cheaper than full `USE_VIDEO`). Requires `FAL_KEY`. Ignored when `USE_VIDEO=true`.
```

- [ ] **Step 2: Update pipeline stage 4 description**

Find the existing stage 4 description:

```markdown
4. `clips`: Generate scene images (Pollinations primary, Imagen fallback) OR videos (fal.ai LTX Video when `USE_VIDEO=true`)
```

Replace with:

```markdown
4. `clips`: Generate scene images (Pollinations primary, Imagen fallback), OR all-videos (fal.ai LTX Video when `USE_VIDEO=true`), OR hook-only video (fal.ai LTX Video for scene 1 only when `USE_VIDEO_HOOK=true`)
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude): document USE_VIDEO_HOOK in env vars and pipeline stages"
```

---

## Task 14: Full regression sweep

**Files:** None (verification only)

- [ ] **Step 1: Run the entire fast test suite**

Run: `pytest -v -m "not slow"`
Expected: ALL PASS (all existing tests + 6 new tests).

- [ ] **Step 2: Quick smoke — import-only check**

Run: `python -c "from app.services import video_service; print(video_service._HOOK_PROMPT_SUFFIX); print(video_service.generate_hook_clip); print(video_service.generate_hook_plus_images)"`

Expected: prints the suffix and two function repr strings; no import errors.

- [ ] **Step 3: Config toggle check**

Run: `USE_VIDEO_HOOK=true python -c "from app import config; print('hook=', config.USE_VIDEO_HOOK, 'video=', config.USE_VIDEO)"`

Expected: `hook= True video= False`

- [ ] **Step 4: Final commit (if any doc drift was touched)**

If no changes: skip. Otherwise:

```bash
git status
# If anything is dirty, commit with an appropriate message.
```

---

## Out of Scope (Do Not Implement Here)

- LLM-generated `hook_video_prompt` field — captured in spec "Alternatives".
- Unifying flags into `VIDEO_MODE=none|hook|all` — rejected in spec.
- Per-scene video selection (scene N is video) — future work.
- Changes to `render_service` — already handles mixed `.mp4 + .png` scene paths.
- Database migrations — none required.
