"""Daily clips pipeline and CLI, with every external stage faked."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.clips import __main__ as cli
from app.clips.formats import FORMATS, SHORT_MAX_S
from app.clips.models import CandidateClip
from app.clips.pipeline import Deps, edition_key, run_daily, window_end
from app.clips.profiles import load_profile
from app.clips.render import BaseRender, LanguageRender
from app.db.database import Base, get_engine, get_session
from app.db.models import Clip, Edition

DAY = date(2026, 11, 19)
END = datetime(2026, 11, 19, 11, 0, tzinfo=timezone.utc)  # 08:00 BRT
TOP, FUNNY = FORMATS["top_daily"], FORMATS["funny_daily"]
EN = load_profile("five-star-clips")
BR = load_profile("five-star-clips-br")
OUTRO_9X16_S = {EN.key: 6.3, BR.key: 6.8}


@pytest.fixture
def session(tmp_path):
    eng = get_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(eng)
    with get_session(eng) as s:
        yield s


def _candidates(n_top: int = 12, n_funny: int = 10, *, hours_ago: float = 3) -> list[CandidateClip]:
    made = END - timedelta(hours=hours_ago)
    out = []
    for i in range(n_top + n_funny):
        out.append(CandidateClip(
            source="twitch", source_clip_id=f"c{i}", url=f"https://clips.twitch.tv/c{i}",
            broadcaster_name=f"streamer{i}", broadcaster_url=None, title="funny" if i >= n_top else "play",
            view_count=10_000 - i, duration_s=30.0, created_at=made,
        ))
    return out


class FakeSource:
    def __init__(self, candidates):
        self.candidates = candidates
        self.windows = []

    def fetch(self, since, until):
        self.windows.append((since, until))
        return self.candidates


class FakeClassifier:
    """Titles "funny" become funny clips; everything else is a play."""

    def __init__(self):
        self.seen = []

    def classify_pending(self, session, clips):
        for clip in clips:
            if clip.classified_at is not None:
                continue
            self.seen.append(clip.id)
            funny = clip.title == "funny"
            clip.category = "funny" if funny else "play"
            clip.funny_score = 8.0 if funny else 1.0
            clip.hype_score = 5.0
            clip.needs_speech = False
            clip.classified_at = datetime.now(timezone.utc)
        session.commit()
        return {"gemini": len(self.seen), "groq": 0, "failed": 0}


class Harness:
    def __init__(self, tmp_path, candidates=None):
        self.tmp = tmp_path
        self.source = FakeSource(candidates if candidates is not None else _candidates())
        self.classifier = FakeClassifier()
        self.base_calls = []
        self.lang_calls = []
        self.fail_format: str | None = None

    def download(self, session, clips):
        for clip in clips:
            path = self.tmp / "raw" / f"{clip.source_clip_id}.mp4"
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b"raw")
            clip.local_path = str(path)
        session.commit()
        return list(clips)

    def outro(self, profile, aspect):
        path = self.tmp / "outro" / f"{profile.key}_{aspect}.mp4"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"o")
        return path

    def probe(self, path):
        key, aspect = Path(path).stem.rsplit("_", 1)
        assert aspect == "9x16"
        return OUTRO_9X16_S[key]

    def render_base(self, plan, clips, edition_dir):
        if plan.format_key == self.fail_format:
            raise RuntimeError("ffmpeg exploded")
        self.base_calls.append((plan, clips, edition_dir))
        edition_dir.mkdir(parents=True, exist_ok=True)
        n = len(plan.items)
        return BaseRender(
            base_16x9=edition_dir / "base_16x9.mp4",
            base_9x16=edition_dir / "base_9x16.mp4" if plan.short else None,
            offsets=tuple(29.7 * k for k in range(n)),
            duration_s=29.7 * n + 0.3,
            peak_frame=edition_dir / "peak.png",
        )

    def render_language(self, base, *, title, outro_16x9, outro_9x16, lang_dir):
        self.lang_calls.append({"title": title, "lang_dir": lang_dir, "outro_9x16": outro_9x16})
        lang_dir.mkdir(parents=True, exist_ok=True)
        return LanguageRender(lang_dir / "medio.mp4",
                              lang_dir / "short.mp4" if base.base_9x16 else None,
                              lang_dir / "thumb.jpg")

    def deps(self) -> Deps:
        return Deps(
            source=lambda: self.source, download=self.download, classifier=lambda: self.classifier,
            outro=self.outro, render_base=self.render_base, render_language=self.render_language,
            probe=self.probe, now=lambda: END + timedelta(hours=1),
        )

    def run(self, session, **kw):
        kw.setdefault("specs", [TOP, FUNNY])
        kw.setdefault("profiles", [EN, BR])
        return run_daily(session, DAY, editions_dir=self.tmp / "editions", deps=self.deps(), **kw)


# --------------------------------------------------------------------------- #
# Window
# --------------------------------------------------------------------------- #

def test_window_ends_at_eight_in_sao_paulo_in_utc():
    assert window_end(DAY, END + timedelta(hours=5)) == END
    assert window_end(DAY, END + timedelta(hours=5)).tzinfo == timezone.utc


def test_window_end_is_clamped_to_now():
    early = END - timedelta(hours=2)
    assert window_end(DAY, early) == early


def test_harvest_asks_for_the_last_24_hours(session, tmp_path):
    h = Harness(tmp_path)
    h.run(session)
    assert h.source.windows == [(END - timedelta(hours=24), END)]


# --------------------------------------------------------------------------- #
# Full run
# --------------------------------------------------------------------------- #

def test_daily_run_renders_both_editions_for_every_channel(session, tmp_path):
    h = Harness(tmp_path)
    report = h.run(session)

    assert report.ok, report
    assert [(r.format_key, r.outcome) for r in report.editions] == [
        ("top_daily", "rendered"), ("funny_daily", "rendered"),
    ]
    editions = session.query(Edition).order_by(Edition.id).all()
    assert [e.status for e in editions] == ["rendered", "rendered"]
    assert all(e.numbers == {EN.key: 1, BR.key: 1} for e in editions)

    top = editions[0]
    # Countdown: the most viewed clip (c0) plays last and is the Short.
    assert top.items[-1].clip.source_clip_id == "c0"
    assert [i.in_short for i in top.items].count(True) == 1 and top.items[-1].in_short

    titles = sorted(call["title"] for call in h.lang_calls)
    assert "GTA 6 Top Clips of the Day #1" in titles
    assert "GTA 6 Momentos Engraçados #1" in titles


def test_top_and_funny_never_share_a_clip(session, tmp_path):
    Harness(tmp_path).run(session)
    top, funny = session.query(Edition).order_by(Edition.id).all()
    assert not {i.clip_id for i in top.items} & {i.clip_id for i in funny.items}


def test_short_budget_comes_from_the_longest_9x16_outro(session, tmp_path):
    report = Harness(tmp_path).run(session)
    assert report.short_budget_s == pytest.approx(SHORT_MAX_S - max(OUTRO_9X16_S.values()))


def test_metadata_and_status_files_are_written(session, tmp_path):
    Harness(tmp_path).run(session)
    edition_dir = tmp_path / "editions" / edition_key(DAY, "top_daily")
    meta = json.loads((edition_dir / BR.key / "metadata.json").read_text(encoding="utf-8"))
    assert meta["medium"]["title"] == "GTA 6 Melhores Clipes do Dia #1"
    assert "0:00 #8" in meta["medium"]["description"]
    assert meta["short"]["defaultLanguage"] == "pt-BR"
    status = json.loads((edition_dir / "status.json").read_text(encoding="utf-8"))
    assert set(status["stages"]) == {"base", f"language:{EN.key}", f"language:{BR.key}"}
    assert status["error"] is None


# --------------------------------------------------------------------------- #
# Dry run, skips, insufficient material
# --------------------------------------------------------------------------- #

def test_dry_run_plans_without_persisting_or_rendering(session, tmp_path):
    h = Harness(tmp_path)
    report = h.run(session, dry_run=True)
    assert [(r.format_key, r.outcome) for r in report.editions] == [
        ("top_daily", "planned"), ("funny_daily", "planned"),
    ]
    assert all(r.plan is not None for r in report.editions)
    assert session.query(Edition).count() == 0
    assert h.base_calls == [] and h.lang_calls == []


def test_skip_harvest_plans_from_stored_clips(session, tmp_path):
    Harness(tmp_path).run(session, dry_run=True)  # stores the clips
    h = Harness(tmp_path)
    report = h.run(session, dry_run=True, skip_harvest=True)
    assert h.source.windows == []
    assert report.editions[0].outcome == "planned"


def test_skip_classify_leaves_funny_without_candidates(session, tmp_path):
    h = Harness(tmp_path)
    report = h.run(session, dry_run=True, skip_classify=True)
    assert h.classifier.seen == []
    outcomes = {r.format_key: r.outcome for r in report.editions}
    assert outcomes == {"top_daily": "planned", "funny_daily": "skipped"}


def test_insufficient_material_creates_no_edition(session, tmp_path):
    h = Harness(tmp_path, candidates=_candidates(n_top=12, n_funny=0))
    report = h.run(session)
    funny = next(r for r in report.editions if r.format_key == "funny_daily")
    assert funny.outcome == "skipped" and "candidate" in funny.detail
    assert session.query(Edition).filter_by(format_key="funny_daily").count() == 0


def test_clips_outside_the_window_are_ignored(session, tmp_path):
    h = Harness(tmp_path, candidates=_candidates(hours_ago=30))
    report = h.run(session)
    assert all(r.outcome == "skipped" for r in report.editions)


def test_harvest_failure_is_reported_and_planning_continues(session, tmp_path):
    h = Harness(tmp_path)
    h.run(session, dry_run=True)

    class Broken:
        def fetch(self, since, until):
            raise ConnectionError("twitch down")

    deps = replace(h.deps(), source=lambda: Broken())
    report = run_daily(session, DAY, specs=[TOP], profiles=[EN], dry_run=True,
                       editions_dir=tmp_path / "editions", deps=deps)
    assert not report.ok
    assert "twitch down" in report.errors[0]
    assert report.editions[0].outcome == "planned"


def test_no_format_due_does_nothing(session, tmp_path):
    h = Harness(tmp_path)
    report = run_daily(session, date(2026, 11, 18), profiles=[EN], deps=h.deps(),
                       editions_dir=tmp_path / "editions")
    assert report.editions == [] and h.source.windows == []


# --------------------------------------------------------------------------- #
# Re-runs
# --------------------------------------------------------------------------- #

def test_rerun_keeps_the_plan_even_when_views_change(session, tmp_path):
    Harness(tmp_path).run(session)
    first = {e.format_key: [i.clip_id for i in e.items] for e in session.query(Edition)}

    # Views flip: a fresh plan would pick different clips.
    flipped = [c.model_copy(update={"view_count": c.view_count % 7}) for c in _candidates()]
    h = Harness(tmp_path, candidates=flipped)
    report = h.run(session)
    assert report.ok
    assert session.query(Edition).count() == 2
    assert {e.format_key: [i.clip_id for i in e.items] for e in session.query(Edition)} == first
    assert len(h.base_calls) == 2  # render is re-entered; its own cache makes it cheap


def test_rerun_does_not_classify_again(session, tmp_path):
    Harness(tmp_path).run(session)
    h = Harness(tmp_path)
    h.run(session)
    assert h.classifier.seen == []


def test_decided_editions_are_left_alone(session, tmp_path):
    Harness(tmp_path).run(session)
    top = session.query(Edition).filter_by(format_key="top_daily").one()
    top.status = "rejected"
    session.commit()
    h = Harness(tmp_path)
    report = h.run(session)
    outcomes = {r.format_key: r.outcome for r in report.editions}
    assert outcomes == {"top_daily": "rejected", "funny_daily": "rendered"}
    assert [plan.format_key for plan, _, _ in h.base_calls] == ["funny_daily"]


def test_failed_edition_does_not_stop_the_other(session, tmp_path):
    h = Harness(tmp_path)
    h.fail_format = "top_daily"
    report = h.run(session)
    outcomes = {r.format_key: r.outcome for r in report.editions}
    assert outcomes == {"top_daily": "failed", "funny_daily": "rendered"}
    assert not report.ok
    top = session.query(Edition).filter_by(format_key="top_daily").one()
    assert top.status == "planned" and "ffmpeg exploded" in top.error_message
    status = json.loads((tmp_path / "editions" / edition_key(DAY, "top_daily") / "status.json")
                        .read_text(encoding="utf-8"))
    assert status["stages"]["base"]["status"] == "failed"

    # The retry renders it from the stored plan.
    h.fail_format = None
    again = h.run(session)
    assert {r.format_key: r.outcome for r in again.editions}["top_daily"] == "rendered"


def test_numbers_follow_approved_editions(session, tmp_path):
    prev = Edition(format_key="top_daily", edition_date=DAY - timedelta(days=1), status="approved")
    prev.numbers = {EN.key: 4, BR.key: 6}
    session.add(prev)
    session.commit()
    Harness(tmp_path).run(session, specs=[TOP])
    top = session.query(Edition).filter_by(edition_date=DAY).one()
    assert top.numbers == {EN.key: 5, BR.key: 7}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def test_cli_forces_formats_and_dry_run(monkeypatch, session, tmp_path):
    seen = {}

    def fake_run(sess, day, **kw):
        seen.update(day=day, **kw)
        from app.clips.pipeline import DailyReport
        return DailyReport(day=day, window_end=END)

    class Ctx:
        def __enter__(self):
            return session

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(cli, "run_daily", fake_run)
    monkeypatch.setattr(cli, "get_session", lambda: Ctx())
    code = cli.main(["daily", "--date", "2026-11-18", "--dry-run",
                     "--format", "top_daily", "--format", "funny_daily", "--skip-classify"])
    assert code == 0
    assert seen["day"] == date(2026, 11, 18)
    assert [s.key for s in seen["specs"]] == ["top_daily", "funny_daily"]
    assert seen["dry_run"] and seen["skip_classify"] and not seen["skip_harvest"]


def test_cli_rejects_unknown_format():
    with pytest.raises(SystemExit):
        cli.main(["daily", "--format", "nope"])


def test_report_lists_the_selection(session, tmp_path):
    report = Harness(tmp_path).run(session, dry_run=True, specs=[TOP])
    names = {c.id: c.broadcaster_name for c in session.query(Clip)}
    text = cli.format_report(report, names)
    assert "top_daily: planned" in text
    assert "#1  clip" in text and "streamer0" in text and "[Short" in text
