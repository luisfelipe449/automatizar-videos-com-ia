# Viral Titles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make LLM-generated titles (both for full scripts and for proposed angles) more viral and varied by embedding three named title formulas with examples into both system prompts.

**Architecture:** Text-only edit to two string constants in `app/services/script_service.py` — `_SYSTEM_PROMPT` and `_ANGLES_SYSTEM_PROMPT`. No code-path changes, no new modules, no new config. Existing Pydantic validation + retry loop already enforces length and schema.

**Tech Stack:** Python 3.13, Groq / Anthropic Bedrock SDK, pytest (for regression check only — prompt content itself is not unit-testable).

**Spec:** `docs/superpowers/specs/2026-05-10-viral-titles-design.md`

---

## Design note: why no TDD here

The change is content, not behavior. The only assertion I could write is "does the new string appear in the prompt constant" — a tautology that adds no signal and tightly couples the test to the prompt wording, making future tweaks painful.

Instead:
- Regression signal comes from the existing test suite (`tests/test_script_service.py`), which mocks the Groq response and doesn't care what the prompt contains. It must still pass after the edit.
- Quality signal (did titles actually improve?) is manual — run a couple of jobs after merging and read the outputs.

One task, small and focused. Commit once.

---

### Task 1: Inject Title formulas section into both system prompts

**Files:**
- Modify: `app/services/script_service.py` — `_SYSTEM_PROMPT` constant (around lines 35–98) and `_ANGLES_SYSTEM_PROMPT` constant (around lines 119–149)
- Regression test: `tests/test_script_service.py` (no edits; must continue to pass)

- [ ] **Step 1: Confirm current line numbers of both constants**

Run: `grep -n "_SYSTEM_PROMPT = \|_ANGLES_SYSTEM_PROMPT = " app/services/script_service.py`

Expected output (line numbers may shift by a line or two):
```
35:_SYSTEM_PROMPT = """You are a scriptwriter for viral English-language YouTube Shorts.
119:_ANGLES_SYSTEM_PROMPT = """You propose distinct viral angles for short-form video themes.
```

If the line numbers drift significantly, re-read the file and adjust the Edit `old_string` anchors accordingly. The content-based anchors below are the source of truth, not the line numbers.

- [ ] **Step 2: Edit `_SYSTEM_PROMPT` — insert Title formulas block before "Hard numeric constraints"**

Use the Edit tool to replace this exact block (currently right above the `Hard numeric constraints` header):

`old_string`:
```
- NO recognisable faces of real people

Hard numeric constraints (check these BEFORE returning your JSON):
```

`new_string`:
```
- NO recognisable faces of real people

Title formulas — pick ONE that best fits the topic (vary across videos, don't default to the same one):

A. Curiosity gap + number
   Good: "9 Space Facts That Broke My Brain"
   Good: "5 Brazilians Who Literally Changed The World"
   Good: "The #1 Shark Fact Nobody Tells You"

B. Shock + emotional superlative
   Good: "These Space Facts Are INSANE"
   Good: "This Shark Fact Will RUIN Your Sleep"
   Good: "Pelé Did Something NOBODY Talks About"

C. Question / viewer challenge
   Good: "Did You Know Space Smells Like Steak?"
   Good: "Bet You Can't Guess Fact #7"
   Good: "Can You Name The Greatest Brazilian Ever?"

Title rules:
- Every promise in the title MUST be genuinely supported by the narration — no misleading clickbait.
- At most ONE word in CAPS per title (e.g. INSANE, NEVER, RUIN, #1). NEVER put the whole title in CAPS.
- NO emojis in the title.
- Avoid dry/generic phrasings like "Amazing Facts About X", "Top 5 Greatest X", "Cool Facts".
- Avoid generic clickbait like "YOU WON'T BELIEVE THIS!!!" that doesn't describe the actual content.

Hard numeric constraints (check these BEFORE returning your JSON):
```

- [ ] **Step 3: Edit `_ANGLES_SYSTEM_PROMPT` — insert Title formulas block right before "Hard rules"**

Use the Edit tool to replace this exact block:

`old_string`:
```
{
  "angles": [
    {
      "title": "short hook-style title, ≤80 chars",
      "prompt": "1-2 sentence prompt the scriptwriter will read as the topic"
    }
  ]
}

Hard rules:
```

`new_string`:
```
{
  "angles": [
    {
      "title": "short hook-style title, ≤80 chars",
      "prompt": "1-2 sentence prompt the scriptwriter will read as the topic"
    }
  ]
}

Title formulas — pick ONE per angle that best fits it (vary across the N angles, don't default to the same formula):

A. Curiosity gap + number
   Good: "9 Space Facts That Broke My Brain"
   Good: "5 Brazilians Who Literally Changed The World"
   Good: "The #1 Shark Fact Nobody Tells You"

B. Shock + emotional superlative
   Good: "These Space Facts Are INSANE"
   Good: "This Shark Fact Will RUIN Your Sleep"
   Good: "Pelé Did Something NOBODY Talks About"

C. Question / viewer challenge
   Good: "Did You Know Space Smells Like Steak?"
   Good: "Bet You Can't Guess Fact #7"
   Good: "Can You Name The Greatest Brazilian Ever?"

Title rules:
- The title must honestly describe what the angle is about — no misleading clickbait.
- At most ONE word in CAPS per title (e.g. INSANE, NEVER, RUIN, #1). NEVER put the whole title in CAPS.
- NO emojis in the title.
- Avoid dry/generic phrasings like "Amazing Facts About X", "Top 5 Greatest X", "Cool Facts".
- Across the N angles, prefer using at least two different formulas to add variety.

Hard rules:
```

- [ ] **Step 4: Run the regression suite for the script service**

Run: `pytest -v tests/test_script_service.py`

Expected: all tests pass. The suite mocks the Groq response and doesn't inspect prompt text, so edits to prompt constants MUST NOT break anything. If any test fails, the edit broke something structurally (e.g. accidentally corrupted Python syntax) — fix before committing.

- [ ] **Step 5: Quick syntax sanity check**

Run: `python -c "from app.services.script_service import _SYSTEM_PROMPT, _ANGLES_SYSTEM_PROMPT; print(len(_SYSTEM_PROMPT), len(_ANGLES_SYSTEM_PROMPT))"`

Expected: two integers printed, both larger than before the edit (the file now contains more text). No ImportError, no SyntaxError, no stray traceback.

- [ ] **Step 6: Commit**

```bash
git add app/services/script_service.py docs/superpowers/plans/2026-05-10-viral-titles.md
git commit -m "feat(scripts): inject viral title formulas into script and angles prompts"
```

---

## Self-review

**Spec coverage:**
- ✅ Edit `_SYSTEM_PROMPT` to add Title formulas — Step 2
- ✅ Edit `_ANGLES_SYSTEM_PROMPT` to add Title formulas — Step 3
- ✅ Three formulas (A/B/C) with 3 examples each — present in both steps
- ✅ Guardrails (no clickbait, max 1 CAPS word, no emojis) — present in both steps
- ✅ Contra-examples in the "Avoid" bullets — present in both steps
- ✅ Regression test via existing suite — Step 4
- ✅ Non-scope (description, narration, tags, config, temperature) — untouched

**Placeholder scan:** no TBDs, no "implement later", every step has exact content.

**Type consistency:** only string edits; no type surface to keep consistent.
