"""Clip classifier: what kind of moment a clip is, and how good.

Gemini watches the 480p proxy, sound included, and returns a `ClipVerdict`. When
Gemini fails for a clip, or its quota runs out for the rest of the run,
Groq judges from the title alone. A Groq verdict is stored with
`needs_speech = True` because nothing watched the video, which makes the
ranking discount it.

Only clips with `classified_at IS NULL` and a downloaded file are judged, so
re-running a day never pays for the same clip twice.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from app import config
from app.clips.download import proxy_path_for
from app.db.models import Clip
from app.utils.retries import default_retry

_LOG_PREFIX = "[clips.classify]"

Category = Literal["funny", "fail", "play", "wtf", "other"]

SYSTEM_PROMPT = """\
You judge short Twitch clips of streamers playing GTA 6 for a highlights \
channel. Reply with JSON only, no prose:
{"category": "funny" | "fail" | "play" | "wtf" | "other",
 "funny_score": 0-10, "hype_score": 0-10, "needs_speech": true | false}

category:
- funny: comedic, absurd or lucky moment
- fail: the player's own blunder or mistake
- play: skilful, clutch or impressive action
- wtf: bizarre glitch, physics or AI behaviour
- other: nothing notable (menus, loading, just talking, idle driving)
funny_score: how funny to a general audience, ignoring the streamer's fame.
hype_score: how exciting or impressive it is to watch.
needs_speech: true when the moment only lands if you understand what the \
streamer says; false when the gameplay or the reaction alone carries it.
"""


class ClipVerdict(BaseModel):
    category: Category
    funny_score: float = Field(ge=0, le=10)
    hype_score: float = Field(ge=0, le=10)
    needs_speech: bool


class ClassifierQuotaExceeded(RuntimeError):
    """The classifier's allowance is spent; retrying before the reset is pointless."""

    retryable = False


_QUOTA_MARKERS = ("resource_exhausted", "quota")
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _is_quota_error(exc: BaseException) -> bool:
    # Same rule as tts_service: a spent allowance and ordinary throttling both
    # arrive as 429; only the quota one carries these markers.
    message = str(exc).lower()
    return any(marker in message for marker in _QUOTA_MARKERS)


_OVERLOAD_MARKERS = ("503", "504", "unavailable", "deadline", "timed out", "timeout", "overloaded")


def _is_overload(exc: BaseException) -> bool:
    message = f"{type(exc).__name__} {exc}".lower()
    return any(marker in message for marker in _OVERLOAD_MARKERS)


def parse_verdict(raw: str) -> ClipVerdict:
    return ClipVerdict.model_validate(json.loads(_FENCE.sub("", raw.strip())))


def _context(clip: Clip) -> str:
    return (
        f"Clip title (written by a viewer, may be misleading): {clip.title!r}\n"
        f"Stream language: {clip.language or 'unknown'}\n"
        f"Duration: {clip.duration_s:.0f}s"
    )


# --------------------------------------------------------------------------- #
# Judges
# --------------------------------------------------------------------------- #

class VideoJudge(Protocol):
    model: str

    def judge(self, video: bytes, context: str, correction: str | None = None) -> str: ...


class TextJudge(Protocol):
    model: str

    def judge(self, context: str) -> str: ...


class GeminiJudge:
    """Tries each model in order; moves on when one is overloaded.

    Overload (503/504) and timeouts are common on launch days and clear up
    on their own, so a busy model hands the clip to the next one instead of
    retrying it. A spent quota stops the whole chain: it is per key, not per
    model. `last_model` names the model that produced the last answer.
    """

    def __init__(self, api_key: str, models: list[str], *, timeout_s: int = 60) -> None:
        from google import genai

        if not models:
            raise ValueError("GeminiJudge needs at least one model")
        self._client = genai.Client(api_key=api_key)
        self.models = list(models)
        self.model = self.models[0]
        self.last_model = self.model
        self._timeout_ms = timeout_s * 1000

    def judge(self, video: bytes, context: str, correction: str | None = None) -> str:
        from google.genai import types

        prompt = context if correction is None else f"{context}\n\n{correction}"
        config_ = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
            temperature=0.2,
            http_options=types.HttpOptions(timeout=self._timeout_ms),
        )
        contents = [types.Part.from_bytes(data=video, mime_type="video/mp4"), prompt]
        failures: list[str] = []
        for model in self.models:
            try:
                response = self._client.models.generate_content(
                    model=model, contents=contents, config=config_,
                )
            except Exception as exc:  # noqa: BLE001 — classify, then move on or re-raise
                if _is_quota_error(exc):
                    raise ClassifierQuotaExceeded(f"Gemini quota exhausted: {exc}") from exc
                if not _is_overload(exc):
                    raise
                failures.append(f"{model}: {str(exc)[:80]}")
                continue
            self.last_model = model
            return response.text or ""
        raise RuntimeError(f"every Gemini model is unavailable: {failures}")


class GroqJudge:
    def __init__(self, api_key: str, model: str) -> None:
        from groq import Groq

        self._client = Groq(api_key=api_key)
        self.model = model

    @default_retry
    def judge(self, context: str) -> str:
        response = self._client.chat.completions.create(
            model=self.model,
            response_format={"type": "json_object"},
            temperature=0.2,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"{context}\nYou cannot see the video; judge from the title."},
            ],
        )
        return response.choices[0].message.content or ""


# --------------------------------------------------------------------------- #
# Classifier
# --------------------------------------------------------------------------- #

class Classifier:
    def __init__(self, gemini: VideoJudge | None, groq: TextJudge | None) -> None:
        self._gemini = gemini
        self._groq = groq
        self._gemini_exhausted = False

    @classmethod
    def from_env(cls) -> "Classifier":
        gemini = GeminiJudge(config.require("GEMINI_API_KEY"), config.CLIPS_CLASSIFIER_MODELS)
        groq = GroqJudge(config.GROQ_API_KEY, config.GROQ_MODEL) if config.GROQ_API_KEY else None
        return cls(gemini, groq)

    def _ask_gemini(self, clip: Clip) -> ClipVerdict:
        video = proxy_path_for(Path(clip.local_path)).read_bytes()
        context = _context(clip)
        raw = self._gemini.judge(video, context)
        try:
            return parse_verdict(raw)
        except (ValidationError, ValueError) as exc:
            correction = (
                f"Your previous answer was invalid ({exc.__class__.__name__}: {exc}). "
                f"Reply again with only the JSON object."
            )
            return parse_verdict(self._gemini.judge(video, context, correction))

    def _ask_groq(self, clip: Clip) -> ClipVerdict:
        verdict = parse_verdict(self._groq.judge(_context(clip)))
        return verdict.model_copy(update={"needs_speech": True})

    def _classify_one(self, clip: Clip) -> tuple[ClipVerdict, str, str] | None:
        """(verdict, model that produced it, which judge) or None when both failed."""
        tag = f"{clip.source}:{clip.source_clip_id}"
        if self._gemini is not None and not self._gemini_exhausted:
            try:
                verdict = self._ask_gemini(clip)
                return verdict, getattr(self._gemini, "last_model", self._gemini.model), "gemini"
            except ClassifierQuotaExceeded as exc:
                self._gemini_exhausted = True
                print(f"{_LOG_PREFIX} {exc} — remaining clips go to Groq")
            except Exception as exc:  # noqa: BLE001 — one bad clip must not stop the run
                print(f"{_LOG_PREFIX} gemini failed on {tag}: {exc}")
        if self._groq is None:
            return None
        try:
            return self._ask_groq(clip), self._groq.model, "groq"
        except Exception as exc:  # noqa: BLE001
            print(f"{_LOG_PREFIX} groq failed on {tag}: {exc}")
            return None

    def classify_pending(self, session: Session, clips: list[Clip]) -> dict[str, int]:
        """Judge every downloaded, unclassified clip. Returns counts per judge.

        The gemini/groq split is the signal for turning on Gemini billing:
        a run where most clips went to Groq means the free tier is too small.
        """
        stats = {"gemini": 0, "groq": 0, "failed": 0}
        for clip in clips:
            if clip.classified_at is not None or not clip.local_path:
                continue
            result = self._classify_one(clip)
            if result is None:
                stats["failed"] += 1
                continue
            verdict, model, judge = result
            clip.category = verdict.category
            clip.funny_score = verdict.funny_score
            clip.hype_score = verdict.hype_score
            clip.needs_speech = verdict.needs_speech
            clip.classified_by = model
            clip.classified_at = datetime.now(timezone.utc)
            stats[judge] += 1
            session.commit()
        print(f"{_LOG_PREFIX} classified: {stats}")
        return stats
