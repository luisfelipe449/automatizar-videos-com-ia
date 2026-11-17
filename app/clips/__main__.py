"""Clips-mode command line.

    python -m app.clips daily [--date YYYY-MM-DD] [--dry-run] [--format KEY ...]
                              [--skip-harvest] [--skip-classify]

`daily` produces the editions due on the date (default: today in Sao Paulo).
`--format` forces formats regardless of the calendar, e.g. for a dry run
before launch day, when no format is due yet. `--dry-run` stops after
planning and prints the selection; harvest, download and classification
still run and are stored. Classification calls Gemini, which is billed;
`--skip-classify` plans from views alone (Funny then finds no candidates).

Exit status is 0 when every stage and edition succeeded, else 1.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.clips.formats import FORMATS
from app.clips.pipeline import DailyReport, run_daily
from app.db.database import get_session


def _today_brt() -> date:
    return datetime.now(ZoneInfo("America/Sao_Paulo")).date()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.clips")
    sub = parser.add_subparsers(dest="command", required=True)
    daily = sub.add_parser("daily", help="produce the editions due on a date")
    daily.add_argument("--date", type=date.fromisoformat, default=None,
                       help="edition date, YYYY-MM-DD (default: today in Sao Paulo)")
    daily.add_argument("--dry-run", action="store_true", help="stop after planning")
    daily.add_argument("--format", dest="formats", action="append", choices=sorted(FORMATS),
                       help="force this format (repeatable); default: formats due on the date")
    daily.add_argument("--skip-harvest", action="store_true", help="plan from clips already stored")
    daily.add_argument("--skip-classify", action="store_true", help="do not call the classifier")
    return parser


def format_report(report: DailyReport, clip_names: dict[int, str]) -> str:
    lines = [f"Day {report.day} (window ends {report.window_end:%Y-%m-%d %H:%M} UTC)"]
    if report.harvested is not None:
        lines.append(f"  harvested: new={report.harvested[0]} updated={report.harvested[1]}")
    lines.append(f"  downloaded and ready: {report.downloaded}")
    if report.classified:
        lines.append(f"  classified: {report.classified}")
    if report.short_budget_s is not None:
        lines.append(f"  Short clip budget: {report.short_budget_s:.1f} s")
    for result in report.editions:
        head = f"  {result.format_key}: {result.outcome}"
        lines.append(f"{head} ({result.detail})" if result.detail else head)
        if result.plan is not None and result.outcome in ("planned", "rendered"):
            lines.append(f"    {len(result.plan.items)} clips, {result.plan.total_s:.0f} s, countdown order:")
            for item in result.plan.items:
                short = f"  [Short, trim {item.trim_start_s or 0:.1f} s]" if item.in_short else ""
                name = clip_names.get(item.clip_id, "?")
                lines.append(f"      #{item.rank:<2} clip {item.clip_id:<5} {item.duration_s:5.1f} s  {name}{short}")
    for error in report.errors:
        lines.append(f"  ERROR {error}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    day = args.date or _today_brt()
    specs = [FORMATS[key] for key in args.formats] if args.formats else None
    with get_session() as session:
        report = run_daily(
            session, day, specs=specs, dry_run=args.dry_run,
            skip_harvest=args.skip_harvest, skip_classify=args.skip_classify,
        )
        from app.db.models import Clip

        ids = {i.clip_id for r in report.editions if r.plan for i in r.plan.items}
        names = {c.id: c.broadcaster_name for c in session.query(Clip).filter(Clip.id.in_(ids))} if ids else {}
        print(format_report(report, names))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
