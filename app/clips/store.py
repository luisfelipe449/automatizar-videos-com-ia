"""Persisting harvested clips."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.clips.models import CandidateClip
from app.db.models import Clip


def upsert_clips(session: Session, candidates: list[CandidateClip]) -> tuple[int, int]:
    """Insert unseen clips; refresh mutable fields of known ones.

    Only platform-owned fields (views, title) are refreshed. Download and
    classification columns are left alone so a re-harvest never sends a clip
    back through the paid classifier. Within one batch the last occurrence
    of a clip wins. Returns (inserted, updated).
    """
    latest: dict[tuple[str, str], CandidateClip] = {}
    for cand in candidates:
        latest[(cand.source, cand.source_clip_id)] = cand

    inserted = updated = 0
    for (source, clip_id), cand in latest.items():
        row = session.query(Clip).filter_by(source=source, source_clip_id=clip_id).one_or_none()
        if row is None:
            session.add(Clip(
                source=cand.source,
                source_clip_id=cand.source_clip_id,
                url=cand.url,
                broadcaster_name=cand.broadcaster_name,
                broadcaster_url=cand.broadcaster_url,
                title=cand.title,
                language=cand.language,
                view_count=cand.view_count,
                duration_s=cand.duration_s,
                created_at_source=cand.created_at,
            ))
            inserted += 1
        else:
            row.view_count = cand.view_count
            row.title = cand.title
            updated += 1
    session.commit()
    return inserted, updated
