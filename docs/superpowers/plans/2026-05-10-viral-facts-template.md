# Viral Facts Template Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the scriptwriter system prompt with the user's viral facts template and inject each job's title via a `VIDEO_TITLE` line in the user message, so the theme mode (5 videos) produces 5 scripts that share the same structure and differ only by title.

**Architecture:** `_SYSTEM_PROMPT` in `app/services/script_service.py` is rewritten as a fixed viral-facts structure (Opening Hook → Intro Line → 8-12 facts → CTAs → Emotional Hooks → Closing) fused with the existing JSON-schema rules. `generate_script()` gains an optional `video_title` kwarg; the user message includes `VIDEO_TITLE: <title>`. `create-batch` passes `angle.title`; the CLI reuses `--prompt` as the title. Ranking logic (Top-N / rank / rank_title) is removed from the prompt; the Pydantic fields stay nullable for compatibility.

**Tech Stack:** Python 3.13, Pydantic v2, Groq/Bedrock SDK, pytest.

**Spec:** `docs/superpowers/specs/2026-05-10-viral-facts-template-design.md`

---

## File Structure

- **Modify** `app/services/script_service.py`
  - Replace `_SYSTEM_PROMPT` with the viral facts template + JSON schema rules.
  - Add `video_title: str | None = None` kwarg to `generate_script()`.
  - Append `VIDEO_TITLE: <title>` to the user message.
- **Modify** `app/orchestrator.py`
  - `_stage_script` passes `video_title=prompt` to `generate_script()`.
- **Modify** `app/web/routes/jobs.py`
  - `create-batch` passes `video_title=angle["title"]` for each angle.
- **Modify** `tests/test_script_service.py`
  - Update happy-path test to check the user message contains `VIDEO_TITLE:`.
  - Add `test_video_title_forwarded_to_user_message`.
  - Add `test_video_title_defaults_to_prompt`.
- **Modify** `tests/test_web_routes.py`
  - Update `test_enqueues_n_jobs_on_success` and its peers' fake `generate_script` signature to accept `video_title` kwarg.
  - Add an assertion that `video_title` matches `angle.title` for each call.
- **Modify** `tests/test_orchestrator.py`
  - Update the fake `generate_script` signature to accept `video_title` kwarg.
  - Add assertion that the orchestrator forwards `prompt` as `video_title`.

---

## Task 1: Add `video_title` kwarg to `generate_script` + `VIDEO_TITLE` in user message

**Files:**
- Modify: `app/services/script_service.py` (function `generate_script`, around lines 394-452)
- Test: `tests/test_script_service.py`

- [ ] **Step 1: Write the failing test for `video_title` forwarding**

Add to `tests/test_script_service.py` (after `test_happy_path_writes_script_json`):

```python
def test_video_title_forwarded_to_user_message(monkeypatch, patched_env):
    from app.services.script_service import generate_script

    _install_fake_groq(monkeypatch, [json.dumps(_VALID_PAYLOAD)])

    generate_script("topic text", video_title="My Awesome Title")

    messages = FakeGroq.last_instance.calls[0]["messages"]
    user_msg = next(m for m in messages if m["role"] == "user")
    assert "Topic for the Short: topic text" in user_msg["content"]
    assert "VIDEO_TITLE: My Awesome Title" in user_msg["content"]


def test_video_title_defaults_to_prompt(monkeypatch, patched_env):
    from app.services.script_service import generate_script

    _install_fake_groq(monkeypatch, [json.dumps(_VALID_PAYLOAD)])

    generate_script("fallback prompt text")

    messages = FakeGroq.last_instance.calls[0]["messages"]
    user_msg = next(m for m in messages if m["role"] == "user")
    assert "VIDEO_TITLE: fallback prompt text" in user_msg["content"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_script_service.py::test_video_title_forwarded_to_user_message tests/test_script_service.py::test_video_title_defaults_to_prompt -v`

Expected: FAIL — `generate_script()` currently has no `video_title` parameter and the user message does not contain `VIDEO_TITLE:`.

- [ ] **Step 3: Update `generate_script()` signature and user message**

In `app/services/script_service.py`, change the signature and the `base_messages` construction inside `generate_script`:

```python
def generate_script(
    prompt: str,
    *,
    job_id: str | None = None,
    video_title: str | None = None,
) -> Script:
    """Generate a YouTube Shorts script from a user prompt.

    `video_title` is the title injected into the user message as
    `VIDEO_TITLE: <title>`. When omitted, falls back to `prompt` so the
    CLI (which has no separate title) still produces a valid message.

    If `job_id` is None, derives one via `filepaths.make_job_id(prompt)`.
    Raises `ScriptGenerationError` on unrecoverable failure.
    """
    import os as _os
    resolved_job_id = job_id or filepaths.make_job_id(prompt)
    resolved_title = video_title if (video_title and video_title.strip()) else prompt
    provider, client = _build_client()

    override_path = _os.getenv(_OVERRIDE_ENV, "").strip()
    if override_path and Path(override_path).is_file():
        system_prompt = Path(override_path).read_text(encoding="utf-8")
        print(f"{_LOG_PREFIX} provider={provider} prompt=OVERRIDE({override_path})")
    else:
        system_prompt = _SYSTEM_PROMPT
        print(f"{_LOG_PREFIX} provider={provider}")

    user_content = (
        f"Topic for the Short: {prompt}\n"
        f"VIDEO_TITLE: {resolved_title}"
    )
    base_messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
```

Leave the rest of `generate_script` (attempt 1 / attempt 2 / persist) unchanged.

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `pytest tests/test_script_service.py::test_video_title_forwarded_to_user_message tests/test_script_service.py::test_video_title_defaults_to_prompt -v`

Expected: PASS.

- [ ] **Step 5: Run the full script_service suite to verify no regression**

Run: `pytest tests/test_script_service.py -v`

Expected: all tests pass. The old happy-path test still passes because it never inspected the user message content.

- [ ] **Step 6: Commit**

```bash
git add app/services/script_service.py tests/test_script_service.py
git commit -m "feat(script): add video_title kwarg to generate_script

Injects VIDEO_TITLE: <title> into the user message; defaults to
prompt when omitted so the CLI (no separate title) keeps working."
```

---

## Task 2: Rewrite `_SYSTEM_PROMPT` as the viral facts template

**Files:**
- Modify: `app/services/script_service.py` (replace `_SYSTEM_PROMPT`, around lines 33-98)
- Test: `tests/test_script_service.py`

- [ ] **Step 1: Add a test asserting the new prompt skeleton is present**

Append to `tests/test_script_service.py`:

```python
def test_system_prompt_contains_viral_facts_structure(monkeypatch, patched_env):
    """Sanity check: the new system prompt exposes the 7-step structure
    and the VIDEO_TITLE hook so the LLM has clear instructions."""
    from app.services.script_service import _SYSTEM_PROMPT

    assert "VIDEO_TITLE" in _SYSTEM_PROMPT
    assert "Opening Hook" in _SYSTEM_PROMPT
    assert "Intro Line" in _SYSTEM_PROMPT
    assert "Fact List" in _SYSTEM_PROMPT
    assert "CTA" in _SYSTEM_PROMPT or "Call-to-Action" in _SYSTEM_PROMPT
    assert "Emotional Hooks" in _SYSTEM_PROMPT
    assert "Closing" in _SYSTEM_PROMPT
    # Ranking logic is gone.
    assert "rank_title" not in _SYSTEM_PROMPT
    assert "Top " not in _SYSTEM_PROMPT
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_script_service.py::test_system_prompt_contains_viral_facts_structure -v`

Expected: FAIL — current `_SYSTEM_PROMPT` mentions `rank_title` and `Top ` and does not have the exact headings.

- [ ] **Step 3: Replace `_SYSTEM_PROMPT`**

In `app/services/script_service.py`, replace the entire `_SYSTEM_PROMPT = """..."""` block (lines 33-98 in the current file) with:

```python
_SYSTEM_PROMPT = """You are a scriptwriter for viral English-language YouTube Shorts.

The user message contains TWO lines:
  Topic for the Short: <the topic the user gave>
  VIDEO_TITLE: <the exact title this Short must use>

Use `VIDEO_TITLE` verbatim as the Short's `title`. If it exceeds 100 characters, shorten it minimally while preserving the hook; otherwise keep it as-is.

Build a viral facts video around the topic, titled with VIDEO_TITLE, following this EXACT structure:

1. Opening Hook: an attention-grabbing statement or question that sparks curiosity about the topic.
2. Intro Line: a line like "Here are 10 facts to mess your brain" or "Here are 9 crazy facts you didn't know".
3. Fact List: 8 to 12 interesting facts. State each fact clearly and concisely. Occasionally append parentheticals like "(Try this!)" or "(Try it!)" where applicable. Vary fact types across biology, history, geography, psychology, physics, animals, culture — mix well-known with lesser-known.
4. CTA Insertions: sprinkle natural Calls-to-Action throughout, e.g. "if you want to be smarter every day — like and subscribe", "studies show that if you like and subscribe, you may become smarter".
5. Emotional Hooks: include dramatic phrases like "the last one amazed me", "the last will blow your mind", "you didn't know this".
6. Variety: cover diverse fact categories — physical/biological (organs, DNA, animals), geographic (landmarks, locations), historical (alphabet, traditions), interactive challenges, psychological/cultural.
7. Closing: end with an emotional statement or a reference back to the most impressive fact.

Tone: casual, conversational, fast-paced — suitable for quick reading or narration.

Your output is a SINGLE valid JSON object — no prose, no markdown fences, no trailing text — matching EXACTLY this schema:

{
  "title": "string (≤100 chars, derived from VIDEO_TITLE)",
  "description": "string ending with #Shorts",
  "tags": ["3 to 8 relevant tags, 1-30 chars each, English"],
  "narration": "the full narration, English, casual conversational tone",
  "scenes": [
    {
      "id": 1,
      "scene_text": "the exact line of narration this scene covers (one fact or one structural beat)",
      "image_prompt": "detailed cinematic visual prompt for this beat",
      "duration_seconds": 5,
      "rank": null,
      "rank_title": null
    }
  ]
}

One scene per fact plus one scene for each structural beat (hook, intro, CTAs, closing). For every scene, craft an `image_prompt` that is:
- cinematic, realistic
- vertical 9:16 framing
- matches the emotion of the line
- keeps characters/subjects consistent across recurring scenes
- contains NO text, captions, watermarks, or logos
- contains NO recognisable faces of real people

Hard numeric constraints (verify BEFORE emitting JSON):
- sum(duration_seconds) must be between 45 and 58 seconds. NEVER above 58. If your draft sums to more than 58, remove scenes or shorten durations until it fits. Add them up yourself before emitting the JSON.
- 9 to 13 scenes total, each 3 to 8 seconds.
- Scene ids contiguous starting at 1.
- narration: English, 80 to 120 words total, casual conversational pacing.
- title: ≤100 chars, derived from VIDEO_TITLE.
- description: must end with "#Shorts".
- tags: 3 to 8 items, each 1 to 30 chars.
- rank and rank_title MUST be null in every scene.
- DO NOT include a job_id field — it is injected by the pipeline.
"""
```

- [ ] **Step 4: Run the new prompt test to verify it passes**

Run: `pytest tests/test_script_service.py::test_system_prompt_contains_viral_facts_structure -v`

Expected: PASS.

- [ ] **Step 5: Run the full script_service suite**

Run: `pytest tests/test_script_service.py -v`

Expected: all tests pass. Existing tests that mock JSON responses (not prompt text) are unaffected.

- [ ] **Step 6: Commit**

```bash
git add app/services/script_service.py tests/test_script_service.py
git commit -m "feat(script): replace system prompt with viral facts template

Fuses the fixed 7-step viral-facts structure (hook → intro → 8-12
facts → CTAs → emotional hooks → closing) with the existing JSON
schema rules. Removes Top-N ranking logic from the prompt; the
rank/rank_title Pydantic fields stay nullable for compatibility."
```

---

## Task 3: Forward `video_title` from the orchestrator (CLI path)

**Files:**
- Modify: `app/orchestrator.py` (function `_stage_script`, around line 140)
- Test: `tests/test_orchestrator.py`

- [ ] **Step 1: Update the fake in `tests/test_orchestrator.py` to accept the new kwarg + assert forwarding**

In `tests/test_orchestrator.py`, locate the `fake_generate_script` at line ~66 and update it to accept `video_title` and record it:

```python
script_holder: dict[str, Script] = {}
captured_video_titles: list[str | None] = []

def fake_generate_script(
    prompt: str,
    *,
    job_id: str | None = None,
    video_title: str | None = None,
) -> Script:
    calls["script"] += 1
    captured_video_titles.append(video_title)
    resolved = job_id or filepaths.make_job_id(prompt)
    # ... (rest of the existing body unchanged)
```

Then add an assertion near the end of the same test (after `run_pipeline(...)` is invoked) that the orchestrator forwarded `prompt` as `video_title`:

```python
assert captured_video_titles == [prompt]
```

(Use whichever local name the test uses for the prompt — adjust the assertion to match. If the test uses a literal like `"test prompt"`, assert `captured_video_titles == ["test prompt"]`.)

- [ ] **Step 2: Run the orchestrator tests to verify they fail**

Run: `pytest tests/test_orchestrator.py -v`

Expected: FAIL — the orchestrator currently does not forward `video_title`, so `captured_video_titles` is `[None]`.

- [ ] **Step 3: Update `_stage_script` in `app/orchestrator.py`**

Replace the body of `_stage_script` (around line 140):

```python
def _stage_script(prompt: str, job_id: str) -> Script:
    # CLI/theme share the same scriptwriter. In CLI mode there's no
    # separate title, so the prompt plays double duty: topic + title.
    return generate_script(prompt, job_id=job_id, video_title=prompt)
```

- [ ] **Step 4: Run the orchestrator tests to verify they pass**

Run: `pytest tests/test_orchestrator.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(orchestrator): forward prompt as video_title to generate_script

In CLI mode there is no separate title, so the user prompt becomes
the VIDEO_TITLE injected into the scriptwriter user message."
```

---

## Task 4: Forward `angle.title` from `create-batch` (theme mode)

**Files:**
- Modify: `app/web/routes/jobs.py` (inside `create_batch`, around line 199-210)
- Test: `tests/test_web_routes.py`

- [ ] **Step 1: Update the fakes in `tests/test_web_routes.py` to accept + record `video_title`**

Three tests use a `fake_generate_script` that only accepts `prompt, *, job_id`. Update each signature to also accept `video_title` and record it where relevant:

In `test_enqueues_n_jobs_on_success` (around line 309), replace:

```python
calls: list[str] = []

def fake_generate_script(prompt, *, job_id=None):
    calls.append(prompt)
    return None
```

with:

```python
calls: list[str] = []
captured_titles: list[str | None] = []

def fake_generate_script(prompt, *, job_id=None, video_title=None):
    calls.append(prompt)
    captured_titles.append(video_title)
    return None
```

And add an assertion after the existing `assert len(calls) == 5` line:

```python
assert captured_titles == [a["title"] for a in _FAKE_ANGLES]
```

In `test_aborts_on_script_failure_no_jobs_enqueued` (around line 363), replace:

```python
def fake_generate_script(prompt, *, job_id=None):
    call_counter["n"] += 1
    ...
```

with:

```python
def fake_generate_script(prompt, *, job_id=None, video_title=None):
    call_counter["n"] += 1
    ...
```

(body unchanged).

In `test_rejects_insufficient_quota`, `test_rejects_malformed_angles_json`, and `test_rejects_empty_angle_prompt`, each has a `should_not_run(*_a, **_kw)` stub — these already accept any kwargs, so they are fine. No change needed.

- [ ] **Step 2: Run the web-route tests to verify `test_enqueues_n_jobs_on_success` now fails on the title assertion**

Run: `pytest tests/test_web_routes.py::TestCreateBatchEndpoint -v`

Expected: `test_enqueues_n_jobs_on_success` FAILS on the new `captured_titles` assertion (`route doesn't pass video_title yet`). The other tests still pass.

- [ ] **Step 3: Update `create_batch` in `app/web/routes/jobs.py`**

Locate the loop in `create_batch` (around line 199):

```python
    for idx, (job_id, prompt_text) in enumerate(planned, start=1):
        try:
            script_service.generate_script(prompt_text, job_id=job_id)
        except ScriptGenerationError as exc:
            ...
```

Replace the `generate_script` call with:

```python
    for idx, (job_id, prompt_text) in enumerate(planned, start=1):
        try:
            script_service.generate_script(
                prompt_text,
                job_id=job_id,
                video_title=angles[idx - 1]["title"],
            )
        except ScriptGenerationError as exc:
            ...
```

Leave the `except` branch and the enqueue loop unchanged.

- [ ] **Step 4: Run the web-route tests to verify they pass**

Run: `pytest tests/test_web_routes.py::TestCreateBatchEndpoint -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/web/routes/jobs.py tests/test_web_routes.py
git commit -m "feat(web): forward angle.title as video_title in create-batch

Each of the N jobs generated from a theme now shares the same
scriptwriter prompt but a distinct VIDEO_TITLE — the angle's title."
```

---

## Task 5: Final regression run

**Files:** none.

- [ ] **Step 1: Run the complete test suite**

Run: `pytest -v -m "not slow"`

Expected: all non-slow tests pass.

- [ ] **Step 2: Sanity smoke with a real Groq call (optional, skip if no API key)**

Run, replacing the prompt with any short topic:

```bash
python -m app.orchestrator --prompt "curiosidades sobre tubarões" --skip-upload
```

Inspect `assets/generated/<job_id>/script.json`:
- `title` should reflect the prompt (≤100 chars).
- `scenes[*].rank` and `scenes[*].rank_title` should all be `null`.
- Narration should follow hook → intro → 8-12 facts → CTAs → closing.
- `description` ends with `#Shorts`.

If the smoke test fails because of API credentials, skip — the unit tests already cover the behavior.

- [ ] **Step 3: Final commit if anything was tweaked during smoke**

If the smoke test exposed a typo or bug, fix and commit as a follow-up. Otherwise, nothing to commit.

---

## Self-Review

**Spec coverage:**
- Replace `_SYSTEM_PROMPT` → Task 2. ✓
- Add `video_title` kwarg → Task 1. ✓
- Inject `VIDEO_TITLE:` in user message → Task 1. ✓
- Theme mode uses `angle.title` → Task 4. ✓
- CLI uses prompt as title → Task 3. ✓
- Remove ranking logic from prompt → Task 2 (asserted by test). ✓
- Pydantic schema unchanged → no task needed; confirmed by existing tests continuing to pass.
- `SCRIPT_PROMPT_FILE` override preserved → Task 1 keeps the branch intact.
- Tests listed in spec (happy path update, 2 new script tests, web-route assertion, orchestrator assertion) → covered in Tasks 1, 3, 4.

**Placeholder scan:** No "TBD", no "add error handling", no "similar to Task N". Each code block is complete.

**Type consistency:** `video_title: str | None = None` used consistently across `generate_script`, orchestrator fake, web-route fake. `VIDEO_TITLE:` string literal identical across system prompt, user message construction, and tests.
