"""Daily clips run: harvest → download → classify → plan → render.

Every stage is idempotent, so running a day again resumes it:
- harvest upserts, download skips files on disk, classify skips judged clips;
- an edition already in the database keeps its clips (it is not re-planned
  when views change), and the render stages are cached by input hash;
- a rejected or approved edition is left alone.

Editions render into `<editions_dir>/<date>_<format>/`, one subdirectory per
channel profile key holding `medio.mp4`, `short.mp4`, `thumb.jpg` and
`metadata.json`. `status.json` records each stage's outcome and time.

The harvest window ends at 08:00 in Sao Paulo on the edition day (or now,
if earlier). Clip times are stored in UTC without an offset, so every window
bound is converted to UTC before it reaches a query.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time
from pathlib import Path
from typing import Callable, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.clips import render as clip_render
from app.clips.formats import SHORT_MAX_S, FormatSpec, formats_due_on
from app.clips.metadata import assign_numbers, build_chapters, build_metadata
from app.clips.profiles import ChannelProfile, load_all_profiles
from app.clips.select import EditionPlan, PlannedItem, SkippedEdition, plan_day
from app.clips.store import upsert_clips
from app.config import CLIPS_DIR
from app.db.models import Clip, Edition, EditionClip
from app.utils.ffmpeg_helpers import probe_duration

_LOG_PREFIX = "[clips]"
_CUTOFF = dt_time(8, 0)
_CUTOFF_ZONE = ZoneInfo("America/Sao_Paulo")
EDITIONS_DIR = CLIPS_DIR / "editions"
# Held back from the Short budget: the encoded clip and outro each run a few
# hundredths over their nominal length, and `render_language` refuses a Short
# that crosses `SHORT_MAX_S` by any amount.
SHORT_SAFETY_S = 0.5


def _default_source():
    from app.clips.sources.twitch import TwitchSource

    return TwitchSource.from_env()


def _default_classifier():
    from app.clips.classify import Classifier

    return Classifier.from_env()


def _default_download(session: Session, clips: list[Clip]) -> list[Clip]:
    from app.clips.download import ensure_downloaded

    return ensure_downloaded(session, clips)


def _default_outro(profile: ChannelProfile, aspect: str) -> Path:
    from app.clips.outro import render_outro

    return render_outro(profile, aspect)


@dataclass
class Deps:
    """Everything that touches the network, the disk or FFmpeg."""

    source: Callable[[], object] = _default_source
    download: Callable[[Session, list[Clip]], list[Clip]] = _default_download
    classifier: Callable[[], object] = _default_classifier
    outro: Callable[[ChannelProfile, str], Path] = _default_outro
    render_base: Callable[..., clip_render.BaseRender] = clip_render.render_base
    render_language: Callable[..., clip_render.LanguageRender] = clip_render.render_language
    probe: Callable[[Path], float] = probe_duration
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)


@dataclass
class EditionResult:
    format_key: str
    outcome: str  # planned (dry run) | rendered | skipped | failed | approved | rejected
    detail: str = ""
    plan: EditionPlan | None = None


@dataclass
class DailyReport:
    day: date
    window_end: datetime
    harvested: tuple[int, int] | None = None  # (inserted, updated)
    downloaded: int = 0
    classified: dict[str, int] = field(default_factory=dict)
    short_budget_s: float | None = None
    editions: list[EditionResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and all(e.outcome != "failed" for e in self.editions)


def window_end(day: date, now: datetime) -> datetime:
    """08:00 Sao Paulo on `day`, or `now` if that is still ahead; in UTC."""
    cutoff = datetime.combine(day, _CUTOFF, tzinfo=_CUTOFF_ZONE)
    return min(cutoff, now).astimezone(timezone.utc)


def edition_key(day: date, format_key: str) -> str:
    return f"{day.isoformat()}_{format_key}"


def _clips_in_window(session: Session, end: datetime, window: timedelta) -> list[Clip]:
    return (
        session.query(Clip)
        .filter(Clip.created_at_source >= end - window, Clip.created_at_source <= end)
        .all()
    )


def _plan_from_rows(edition: Edition) -> EditionPlan:
    n = len(edition.items)
    return EditionPlan(edition.format_key, tuple(
        PlannedItem(
            clip_id=item.clip_id,
            rank=n - item.position + 1,
            position=item.position,
            duration_s=item.clip.duration_s,
            in_short=item.in_short,
            trim_start_s=item.trim_start_s,
        )
        for item in edition.items
    ))


def _persist(session: Session, day: date, plan: EditionPlan) -> Edition:
    edition = session.query(Edition).filter_by(format_key=plan.format_key, edition_date=day).one_or_none()
    if edition is None:
        edition = Edition(format_key=plan.format_key, edition_date=day, status="planned")
        session.add(edition)
    edition.items = [
        EditionClip(clip_id=i.clip_id, position=i.position, in_short=i.in_short, trim_start_s=i.trim_start_s)
        for i in plan.items
    ]
    session.commit()
    return edition


class _Status:
    """`status.json` of one edition: stage outcomes, timings and the last error."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = {"stages": {}, "error": None}

    def run(self, stage: str, fn: Callable[[], object]):
        started = time.monotonic()
        try:
            result = fn()
        except Exception as exc:
            self.data["stages"][stage] = {"status": "failed", "seconds": round(time.monotonic() - started, 2)}
            self.data["error"] = f"{stage}: {exc.__class__.__name__}: {exc}"
            self._write()
            raise
        self.data["stages"][stage] = {"status": "done", "seconds": round(time.monotonic() - started, 2)}
        self._write()
        return result

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")


def _render_edition(
    session: Session,
    edition: Edition,
    plan: EditionPlan,
    profiles: Sequence[ChannelProfile],
    *,
    editions_dir: Path,
    deps: Deps,
) -> None:
    edition_dir = editions_dir / edition_key(edition.edition_date, edition.format_key)
    status = _Status(edition_dir / "status.json")
    numbers = assign_numbers(session, edition, profiles)
    clips = {item.clip_id: item.clip for item in edition.items}

    base = status.run("base", lambda: deps.render_base(plan, clips, edition_dir))
    ranks = [item.rank for item in sorted(plan.items, key=lambda i: i.position)]
    chapters = build_chapters(base.offsets, ranks, lead_in_s=clip_render.TITLE_CARD_S)

    for profile in profiles:
        number = numbers[profile.key]
        lang_dir = edition_dir / profile.key

        def language(profile=profile, number=number, lang_dir=lang_dir):
            outro_9x16 = deps.outro(profile, "9x16") if base.base_9x16 is not None else None
            return deps.render_language(
                base,
                title=profile.title_for(edition.format_key, number),
                outro_16x9=deps.outro(profile, "16x9"),
                outro_9x16=outro_9x16,
                lang_dir=lang_dir,
            )

        out = status.run(f"language:{profile.key}", language)
        meta = {
            "medium": build_metadata(profile, edition.format_key, number, chapters=chapters).snippet(),
            "short": build_metadata(profile, edition.format_key, number).snippet() if out.short else None,
        }
        (lang_dir / "metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    edition.status = "rendered"
    edition.error_message = None
    edition.rendered_at = datetime.now(timezone.utc)
    session.commit()


def _group_by_window(specs: Sequence[FormatSpec]) -> dict[timedelta, list[FormatSpec]]:
    groups: dict[timedelta, list[FormatSpec]] = {}
    for spec in specs:
        groups.setdefault(spec.window, []).append(spec)
    return groups


def run_daily(
    session: Session,
    day: date,
    *,
    specs: Sequence[FormatSpec] | None = None,
    profiles: Sequence[ChannelProfile] | None = None,
    dry_run: bool = False,
    skip_harvest: bool = False,
    skip_classify: bool = False,
    editions_dir: Path = EDITIONS_DIR,
    deps: Deps | None = None,
) -> DailyReport:
    """Produce the editions due on `day` (or `specs`). See the module docstring."""
    deps = deps or Deps()
    specs = list(specs) if specs is not None else formats_due_on(day)
    profiles = list(profiles) if profiles is not None else load_all_profiles()
    end = window_end(day, deps.now())
    report = DailyReport(day=day, window_end=end)
    if not specs:
        print(f"{_LOG_PREFIX} no format due on {day}")
        return report

    # Harvest covers one day; weekly formats draw on what the dailies stored.
    if not skip_harvest:
        try:
            candidates = deps.source().fetch(end - timedelta(hours=24), end)
            report.harvested = upsert_clips(session, candidates)
            print(f"{_LOG_PREFIX} harvested {len(candidates)} clips (new, updated) = {report.harvested}")
        except Exception as exc:  # noqa: BLE001 — plan from what is already stored
            report.errors.append(f"harvest: {exc.__class__.__name__}: {exc}")
            print(f"{_LOG_PREFIX} harvest failed, planning from stored clips: {exc}")

    widest = max(spec.window for spec in specs)
    ready = deps.download(session, _clips_in_window(session, end, widest))
    report.downloaded = len(ready)

    if not skip_classify:
        try:
            report.classified = deps.classifier().classify_pending(session, ready)
        except Exception as exc:  # noqa: BLE001 — unclassified clips still rank in Top
            report.errors.append(f"classify: {exc.__class__.__name__}: {exc}")
            print(f"{_LOG_PREFIX} classification failed: {exc}")

    # The Short's clip gets what the longest 9:16 outro leaves under the cap,
    # less a safety margin for encoder rounding.
    budget = SHORT_MAX_S
    if any(spec.has_short for spec in specs) and profiles:
        budget = SHORT_MAX_S - SHORT_SAFETY_S - max(deps.probe(deps.outro(p, "9x16")) for p in profiles)
    report.short_budget_s = budget

    ready_ids = {clip.id for clip in ready}
    to_render: list[tuple[Edition, EditionPlan]] = []
    for window, group in _group_by_window(specs).items():
        taken: set[int] = set()
        fresh: list[FormatSpec] = []
        for spec in group:
            existing = session.query(Edition).filter_by(format_key=spec.key, edition_date=day).one_or_none()
            if existing is None or not existing.items:
                fresh.append(spec)
                continue
            taken.update(item.clip_id for item in existing.items)
            if existing.status in ("approved", "rejected"):
                report.editions.append(EditionResult(spec.key, existing.status, "already decided"))
                continue
            plan = _plan_from_rows(existing)
            if dry_run:
                report.editions.append(EditionResult(spec.key, "planned", "existing plan", plan))
            else:
                to_render.append((existing, plan))

        pool = [c for c in _clips_in_window(session, end, window) if c.id in ready_ids and c.id not in taken]
        for result in plan_day(pool, fresh, short_budget_s=budget):
            if isinstance(result, SkippedEdition):
                print(f"{_LOG_PREFIX} {result.format_key}: no edition, {result.reason}")
                report.editions.append(EditionResult(result.format_key, "skipped", result.reason))
            elif dry_run:
                report.editions.append(EditionResult(result.format_key, "planned", "", result))
            else:
                to_render.append((_persist(session, day, result), result))

    for edition, plan in to_render:
        try:
            _render_edition(session, edition, plan, profiles, editions_dir=editions_dir, deps=deps)
        except Exception as exc:  # noqa: BLE001 — one edition's failure must not stop the other
            session.rollback()
            # A re-render may have replaced some files and not others, so a
            # previously rendered edition must not stay approvable. Approved
            # and rejected editions never reach this loop.
            edition.status = "planned"
            edition.error_message = f"{exc.__class__.__name__}: {exc}"
            session.commit()
            print(f"{_LOG_PREFIX} {edition.format_key} failed: {exc}")
            report.editions.append(EditionResult(edition.format_key, "failed", str(exc), plan))
            continue
        report.editions.append(EditionResult(edition.format_key, "rendered", "", plan))
    return report
