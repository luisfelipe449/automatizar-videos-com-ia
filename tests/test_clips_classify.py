"""Clip classifier: Gemini watches the proxy; Groq judges the title as fallback."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.clips.classify import (
    ClassifierQuotaExceeded,
    Classifier,
    ClipVerdict,
    parse_verdict,
)
from app.clips.download import proxy_path_for
from app.db.database import Base, get_engine, get_session
from app.db.models import Clip
from tests.gemini_errors import PER_DAY_429, PER_MINUTE_429


def _verdict(**overrides) -> str:
    data = {"category": "funny", "funny_score": 8, "hype_score": 5, "needs_speech": False}
    data.update(overrides)
    return json.dumps(data)


# --------------------------------------------------------------------------- #
# parse_verdict
# --------------------------------------------------------------------------- #

def test_parse_plain_json():
    v = parse_verdict(_verdict())
    assert v == ClipVerdict(category="funny", funny_score=8, hype_score=5, needs_speech=False)


def test_parse_tolerates_markdown_fence():
    assert parse_verdict("```json\n" + _verdict(category="fail") + "\n```").category == "fail"


@pytest.mark.parametrize("bad", [
    "not json",
    _verdict(category="epic"),
    _verdict(funny_score=11),
    _verdict(hype_score=-1),
    json.dumps({"category": "funny"}),
])
def test_parse_rejects_invalid(bad):
    with pytest.raises((ValidationError, ValueError)):
        parse_verdict(bad)


# --------------------------------------------------------------------------- #
# Classifier
# --------------------------------------------------------------------------- #

class FakeGemini:
    model = "gemini-fake"

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls: list[dict] = []

    def judge(self, video: bytes, context: str, correction: str | None = None) -> str:
        self.calls.append({"video": video, "context": context, "correction": correction})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeGroq:
    model = "groq-fake"

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls: list[str] = []

    def judge(self, context: str) -> str:
        self.calls.append(context)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def session(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    with get_session(engine) as s:
        yield s


def _clip(session, tmp_path, clip_id: str, *, title: str = "he jumped") -> Clip:
    raw = tmp_path / f"{clip_id}.mp4"
    raw.write_bytes(b"raw")
    proxy_path_for(raw).write_bytes(b"proxy-" + clip_id.encode())
    clip = Clip(
        source="twitch", source_clip_id=clip_id, url="u", broadcaster_name="alanzoka",
        title=title, language="pt", view_count=10, duration_s=20.0,
        created_at_source=datetime(2026, 11, 19, tzinfo=timezone.utc), local_path=str(raw),
    )
    session.add(clip)
    session.commit()
    return clip


def test_gemini_verdict_is_stored(session, tmp_path):
    clip = _clip(session, tmp_path, "a")
    gemini = FakeGemini([_verdict(category="wtf", funny_score=3, hype_score=9)])
    stats = Classifier(gemini, FakeGroq([])).classify_pending(session, [clip])

    assert stats == {"gemini": 1, "groq": 0, "failed": 0}
    assert (clip.category, clip.funny_score, clip.hype_score, clip.needs_speech) == ("wtf", 3, 9, False)
    assert clip.classified_by == "gemini-fake"
    assert clip.classified_at is not None
    assert gemini.calls[0]["video"] == b"proxy-a"


def test_gemini_sees_title_and_language_as_context(session, tmp_path):
    clip = _clip(session, tmp_path, "a", title="carro voando")
    gemini = FakeGemini([_verdict()])
    Classifier(gemini, FakeGroq([])).classify_pending(session, [clip])
    context = gemini.calls[0]["context"]
    assert "carro voando" in context and "pt" in context


def test_already_classified_clips_are_skipped(session, tmp_path):
    clip = _clip(session, tmp_path, "a")
    clip.classified_at = datetime.now(timezone.utc)
    clip.category = "play"
    session.commit()
    gemini = FakeGemini([])
    assert Classifier(gemini, FakeGroq([])).classify_pending(session, [clip]) == {"gemini": 0, "groq": 0, "failed": 0}
    assert gemini.calls == []


def test_undownloaded_clips_are_skipped(session, tmp_path):
    clip = _clip(session, tmp_path, "a")
    clip.local_path = None
    session.commit()
    gemini = FakeGemini([])
    assert Classifier(gemini, FakeGroq([])).classify_pending(session, [clip]) == {"gemini": 0, "groq": 0, "failed": 0}


def test_invalid_answer_gets_one_correction_round(session, tmp_path):
    clip = _clip(session, tmp_path, "a")
    gemini = FakeGemini(["garbage", _verdict(category="fail")])
    Classifier(gemini, FakeGroq([])).classify_pending(session, [clip])

    assert clip.category == "fail"
    assert gemini.calls[0]["correction"] is None
    assert gemini.calls[1]["correction"]  # carries the parse error back to the model


def test_two_invalid_answers_fall_back_to_groq(session, tmp_path):
    clip = _clip(session, tmp_path, "a")
    gemini = FakeGemini(["garbage", "still garbage"])
    groq = FakeGroq([_verdict(category="funny", needs_speech=False)])
    stats = Classifier(gemini, groq).classify_pending(session, [clip])

    assert stats == {"gemini": 0, "groq": 1, "failed": 0}
    assert clip.classified_by == "groq-fake"


def test_groq_verdicts_are_marked_as_needing_speech(session, tmp_path):
    """Groq never saw the video, so its verdict is ranked as if the clip needs speech."""
    clip = _clip(session, tmp_path, "a")
    gemini = FakeGemini([RuntimeError("500 internal")])
    groq = FakeGroq([_verdict(needs_speech=False)])
    Classifier(gemini, groq).classify_pending(session, [clip])
    assert clip.needs_speech is True


def test_quota_exhaustion_switches_the_rest_of_the_run_to_groq(session, tmp_path):
    clips = [_clip(session, tmp_path, cid) for cid in ("a", "b", "c")]
    gemini = FakeGemini([_verdict(), ClassifierQuotaExceeded("quota")])
    groq = FakeGroq([_verdict(), _verdict()])

    stats = Classifier(gemini, groq).classify_pending(session, clips)

    assert stats == {"gemini": 1, "groq": 2, "failed": 0}
    assert len(gemini.calls) == 2  # never asked again after the quota error


def test_both_failing_leaves_clip_unclassified(session, tmp_path, capsys):
    clip = _clip(session, tmp_path, "a")
    gemini = FakeGemini([RuntimeError("boom")])
    groq = FakeGroq([RuntimeError("groq down")])

    stats = Classifier(gemini, groq).classify_pending(session, [clip])

    assert stats == {"gemini": 0, "groq": 0, "failed": 1}
    assert clip.classified_at is None
    assert "groq down" in capsys.readouterr().out


def test_without_groq_a_gemini_failure_just_counts_as_failed(session, tmp_path):
    clip = _clip(session, tmp_path, "a")
    stats = Classifier(FakeGemini([RuntimeError("boom")]), None).classify_pending(session, [clip])
    assert stats == {"gemini": 0, "groq": 0, "failed": 1}


def test_only_a_per_day_quota_counts_as_exhausted():
    from app.utils.quota import is_daily_quota_error

    assert is_daily_quota_error(RuntimeError(PER_DAY_429))
    assert not is_daily_quota_error(RuntimeError(PER_MINUTE_429))
    assert not is_daily_quota_error(RuntimeError("429 Too Many Requests"))


def test_per_minute_throttling_does_not_end_gemini_for_the_run(session, tmp_path):
    clips = [_clip(session, tmp_path, cid) for cid in ("a", "b")]
    judge = _judge({"flash": RuntimeError(PER_MINUTE_429)})
    classifier = Classifier(judge, FakeGroq([_verdict(), _verdict()]))

    stats = classifier.classify_pending(session, clips)

    assert stats == {"gemini": 0, "groq": 2, "failed": 0}
    assert judge._client.models.asked == ["flash", "flash"]  # asked again for clip b


# --------------------------------------------------------------------------- #
# GeminiJudge model chain (SDK client faked)
# --------------------------------------------------------------------------- #

class _FakeModels:
    def __init__(self, outcomes: dict[str, object]):
        self.outcomes = outcomes
        self.asked: list[str] = []

    def generate_content(self, *, model, contents, config):
        self.asked.append(model)
        outcome = self.outcomes[model]
        if isinstance(outcome, Exception):
            raise outcome

        class _Resp:
            text = outcome

        return _Resp()


def _judge(outcomes: dict[str, object]):
    from app.clips.classify import GeminiJudge

    judge = GeminiJudge.__new__(GeminiJudge)
    judge.models = list(outcomes)
    judge.model = judge.models[0]
    judge.last_model = judge.model
    judge._timeout_ms = 1000
    judge._client = type("C", (), {"models": _FakeModels(outcomes)})()
    return judge


def test_overloaded_model_hands_over_to_next():
    judge = _judge({
        "flash": RuntimeError("503 UNAVAILABLE high demand"),
        "lite": _verdict(category="play"),
    })
    assert parse_verdict(judge.judge(b"v", "ctx")).category == "play"
    assert judge.last_model == "lite"
    assert judge._client.models.asked == ["flash", "lite"]


def test_deadline_exceeded_counts_as_overload():
    judge = _judge({"flash": RuntimeError("504 DEADLINE_EXCEEDED"), "lite": _verdict()})
    judge.judge(b"v", "ctx")
    assert judge.last_model == "lite"


def test_quota_error_stops_the_chain():
    judge = _judge({
        "flash": RuntimeError(PER_DAY_429),
        "lite": _verdict(),
    })
    with pytest.raises(ClassifierQuotaExceeded):
        judge.judge(b"v", "ctx")
    assert judge._client.models.asked == ["flash"]


def test_per_minute_throttled_model_hands_over_to_next():
    # Per-minute quotas are per model, so the next model still has room.
    judge = _judge({"flash": RuntimeError(PER_MINUTE_429), "lite": _verdict()})
    judge.judge(b"v", "ctx")
    assert judge.last_model == "lite"


def test_non_overload_error_is_not_masked_by_next_model():
    judge = _judge({"flash": ValueError("400 INVALID_ARGUMENT"), "lite": _verdict()})
    with pytest.raises(ValueError, match="INVALID_ARGUMENT"):
        judge.judge(b"v", "ctx")


def test_all_models_overloaded_raises():
    judge = _judge({"flash": RuntimeError("503"), "lite": RuntimeError("503")})
    with pytest.raises(RuntimeError, match="every Gemini model"):
        judge.judge(b"v", "ctx")


def test_classified_by_records_the_model_that_answered(session, tmp_path):
    clip = _clip(session, tmp_path, "a")
    judge = _judge({"flash": RuntimeError("503 UNAVAILABLE"), "lite": _verdict()})

    stats = Classifier(judge, FakeGroq([])).classify_pending(session, [clip])

    assert stats == {"gemini": 1, "groq": 0, "failed": 0}
    assert clip.classified_by == "lite"
