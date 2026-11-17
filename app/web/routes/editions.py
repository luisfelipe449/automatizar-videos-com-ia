"""Clips editions: the day's batch, review, approve/reject, download.

Files are served from `<EDITIONS_DIR>/<date>_<format>/<profile key>/`.
Profile key and piece are checked against fixed sets before any path is
built, so a URL cannot reach outside an edition's directory. The profile
keys are the channel file names, so serving media never parses a profile
and a broken one cannot fail a download.

Parsed profiles are cached until a channel file is added, removed or
modified (names, sizes and mtimes), so edits apply without a restart.

A decision is final: approving moves the episode counter of every channel
(`app.clips.metadata.next_number`), so only a `rendered` edition can be
approved or rejected.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.clips import pipeline
from app.clips import profiles as profiles_module
from app.clips.profiles import ChannelProfile, load_all_profiles
from app.db.models import Edition
from app.web.dependencies import get_db

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")

_BRT = ZoneInfo("America/Sao_Paulo")
# piece in the URL -> (file name, media type)
_PIECES = {
    "medio": ("medio.mp4", "video/mp4"),
    "short": ("short.mp4", "video/mp4"),
    "thumb": ("thumb.jpg", "image/jpeg"),
}


def _edition_dir(edition: Edition) -> Path:
    # Read at call time so tests can point EDITIONS_DIR elsewhere.
    return pipeline.EDITIONS_DIR / pipeline.edition_key(edition.edition_date, edition.format_key)


def _get_edition(db: Session, edition_id: int) -> Edition:
    edition = db.get(Edition, edition_id)
    if edition is None:
        raise HTTPException(status_code=404, detail=f"edition {edition_id} not found")
    return edition


_SAFE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_profiles_cache: dict[str, object] = {"signature": None, "profiles": {}}


def _channel_files() -> tuple[Path, list[Path]]:
    # Read at call time so tests can point CHANNELS_DIR elsewhere.
    base = profiles_module.CHANNELS_DIR
    return base, sorted(base.glob("*.json"))


def _profiles() -> dict[str, ChannelProfile]:
    """Every channel profile by key, re-parsed only when a channel file changes."""
    base, files = _channel_files()
    stats = [(f.name, f.stat()) for f in files]
    signature = (str(base), tuple((name, st.st_size, st.st_mtime_ns) for name, st in stats))
    if _profiles_cache["signature"] != signature:
        _profiles_cache["profiles"] = {p.key: p for p in load_all_profiles(channels_dir=base)}
        _profiles_cache["signature"] = signature
    return _profiles_cache["profiles"]


def _profile_keys() -> set[str]:
    """Profile keys a media URL may name: channel file names, parsed or not."""
    _, files = _channel_files()
    return {f.stem for f in files if _SAFE_KEY.match(f.stem)}


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _channel_view(edition: Edition, profile: ChannelProfile) -> dict:
    lang_dir = _edition_dir(edition) / profile.key
    slot = profile.publish_at(edition.format_key, edition.edition_date)
    return {
        "key": profile.key,
        "name": profile.display_name,
        "language": profile.language,
        "number": edition.numbers.get(profile.key),
        "publish_local": f"{slot:%H:%M} {profile.schedule[edition.format_key].timezone}",
        "publish_brt": f"{slot.astimezone(_BRT):%d/%m %H:%M} BRT",
        "metadata": _read_json(lang_dir / "metadata.json"),
        "pieces": {piece: (lang_dir / name).is_file() for piece, (name, _) in _PIECES.items()},
    }


@router.get("/", response_class=HTMLResponse)
def list_editions(
    request: Request,
    day: date | None = Query(None, alias="date"),
    db: Session = Depends(get_db),
):
    day = day or datetime.now(_BRT).date()
    editions = db.query(Edition).filter_by(edition_date=day).order_by(Edition.id).all()
    recent = [
        row[0] for row in
        db.query(Edition.edition_date).distinct().order_by(Edition.edition_date.desc()).limit(14)
    ]
    return templates.TemplateResponse(
        request=request,
        name="editions.html",
        context={"day": day, "editions": editions, "recent": recent},
    )


@router.get("/{edition_id}", response_class=HTMLResponse)
def edition_detail(request: Request, edition_id: int, db: Session = Depends(get_db)):
    edition = _get_edition(db, edition_id)
    profiles = _profiles()
    channels = [_channel_view(edition, p) for p in profiles.values()]
    n = len(edition.items)
    clips = [
        {"rank": n - item.position + 1, "position": item.position, "clip": item.clip,
         "in_short": item.in_short, "trim": item.trim_start_s}
        for item in edition.items
    ]
    return templates.TemplateResponse(
        request=request,
        name="edition_detail.html",
        context={
            "edition": edition,
            "channels": channels,
            "clips": clips,
            "status": _read_json(_edition_dir(edition) / "status.json"),
        },
    )


def _decide(db: Session, edition_id: int, status: str) -> RedirectResponse:
    edition = _get_edition(db, edition_id)
    if edition.status != "rendered":
        raise HTTPException(
            status_code=409,
            detail=f"edition {edition_id} is {edition.status}; only a rendered edition can be decided",
        )
    edition.status = status
    edition.decided_at = datetime.now(timezone.utc)
    db.commit()
    return RedirectResponse(url=f"/editions/{edition_id}", status_code=303)


@router.post("/{edition_id}/approve")
def approve(edition_id: int, db: Session = Depends(get_db)):
    return _decide(db, edition_id, "approved")


@router.post("/{edition_id}/reject")
def reject(edition_id: int, db: Session = Depends(get_db)):
    return _decide(db, edition_id, "rejected")


def _piece_response(db: Session, edition_id: int, profile_key: str, piece: str, *, attachment: bool):
    edition = _get_edition(db, edition_id)
    if profile_key not in _profile_keys() or piece not in _PIECES:
        raise HTTPException(status_code=404, detail="unknown channel or piece")
    name, media_type = _PIECES[piece]
    path = _edition_dir(edition) / profile_key / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"{piece} not rendered for {profile_key}")
    filename = f"{pipeline.edition_key(edition.edition_date, edition.format_key)}_{profile_key}_{name}"
    return FileResponse(
        path=str(path), media_type=media_type, filename=filename,
        content_disposition_type="attachment" if attachment else "inline",
    )


@router.get("/{edition_id}/media/{profile_key}/{piece}")
def media(edition_id: int, profile_key: str, piece: str, db: Session = Depends(get_db)):
    """Inline, for the page's players and thumbnail."""
    return _piece_response(db, edition_id, profile_key, piece, attachment=False)


@router.get("/{edition_id}/download/{profile_key}/{piece}")
def download(edition_id: int, profile_key: str, piece: str, db: Session = Depends(get_db)):
    return _piece_response(db, edition_id, profile_key, piece, attachment=True)
