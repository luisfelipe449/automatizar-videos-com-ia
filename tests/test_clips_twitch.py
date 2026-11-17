"""Twitch Helix clip source and clip upsert, against a scripted HTTP fake.

Response shapes follow dev.twitch.tv/docs/api/reference (Get Clips, Get
Games, client-credentials token).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from app.clips.models import CandidateClip
from app.clips.sources.twitch import TwitchError, TwitchSource
from app.clips.store import upsert_clips
from app.db.database import Base, get_engine, get_session
from app.db.models import Clip

UNTIL = datetime(2026, 11, 19, 11, 0, tzinfo=timezone.utc)
SINCE = UNTIL - timedelta(hours=24)


class FakeResponse:
    def __init__(self, status: int, payload: dict | None = None, headers: dict | None = None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(f"{self.status_code}", response=self)


class FakeHttp:
    """Records requests; answers from per-endpoint queues."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.queues: dict[str, list[FakeResponse]] = {"token": [], "games": [], "clips": []}

    def _route(self, url: str) -> str:
        path = urlparse(url).path
        if path.endswith("/oauth2/token"):
            return "token"
        if path.endswith("/helix/games"):
            return "games"
        if path.endswith("/helix/clips"):
            return "clips"
        raise AssertionError(f"unexpected url {url}")

    def _answer(self, method: str, url: str, kwargs: dict) -> FakeResponse:
        route = self._route(url)
        self.calls.append((method, route, kwargs))
        queue = self.queues[route]
        assert queue, f"no scripted response left for {route}"
        return queue.pop(0)

    def post(self, url, **kwargs):
        return self._answer("POST", url, kwargs)

    def get(self, url, **kwargs):
        return self._answer("GET", url, kwargs)

    def routed(self, route: str) -> list[dict]:
        return [kw for _, r, kw in self.calls if r == route]


def _token(value: str = "tok-1", expires_in: int = 5000) -> FakeResponse:
    return FakeResponse(200, {"access_token": value, "expires_in": expires_in, "token_type": "bearer"})


def _games(game_id: str = "123456") -> FakeResponse:
    return FakeResponse(200, {"data": [{"id": game_id, "name": "Grand Theft Auto VI"}]})


def _clip_json(clip_id: str, views: int, **overrides) -> dict:
    data = {
        "id": clip_id,
        "url": f"https://clips.twitch.tv/{clip_id}",
        "embed_url": f"https://clips.twitch.tv/embed?clip={clip_id}",
        "broadcaster_id": "999",
        "broadcaster_name": "alanzoka",
        "creator_id": "1",
        "creator_name": "viewer",
        "video_id": "",
        "game_id": "123456",
        "language": "pt",
        "title": f"clip {clip_id}",
        "view_count": views,
        "created_at": "2026-11-19T02:15:00Z",
        "thumbnail_url": "https://example.invalid/t.jpg",
        "duration": 27.4,
        "vod_offset": None,
        "is_featured": False,
    }
    data.update(overrides)
    return data


def _clips(*clips: dict, cursor: str | None = None) -> FakeResponse:
    return FakeResponse(200, {"data": list(clips), "pagination": {"cursor": cursor} if cursor else {}})


def _source(http: FakeHttp, **kw) -> TwitchSource:
    defaults = dict(
        client_id="cid", client_secret="csecret", game_name="Grand Theft Auto VI",
        min_views=50, max_candidates=60, http=http, sleep=lambda s: None,
        now=lambda: UNTIL,
    )
    defaults.update(kw)
    return TwitchSource(**defaults)


# --------------------------------------------------------------------------- #
# fetch
# --------------------------------------------------------------------------- #

def test_fetch_maps_helix_clips_to_candidates():
    http = FakeHttp()
    http.queues["token"] = [_token()]
    http.queues["games"] = [_games()]
    http.queues["clips"] = [_clips(_clip_json("Abc-1", 900))]

    [clip] = _source(http).fetch(SINCE, UNTIL)

    assert clip == CandidateClip(
        source="twitch",
        source_clip_id="Abc-1",
        url="https://clips.twitch.tv/Abc-1",
        broadcaster_name="alanzoka",
        broadcaster_url="https://www.twitch.tv/alanzoka",
        title="clip Abc-1",
        language="pt",
        view_count=900,
        duration_s=27.4,
        created_at=datetime(2026, 11, 19, 2, 15, tzinfo=timezone.utc),
    )


def test_fetch_sends_auth_headers_and_window():
    http = FakeHttp()
    http.queues["token"] = [_token("tok-xyz")]
    http.queues["games"] = [_games("777")]
    http.queues["clips"] = [_clips()]

    _source(http).fetch(SINCE, UNTIL)

    [token_call] = http.routed("token")
    assert token_call["data"] == {
        "client_id": "cid", "client_secret": "csecret", "grant_type": "client_credentials",
    }
    [games_call] = http.routed("games")
    assert games_call["params"] == {"name": "Grand Theft Auto VI"}
    [clips_call] = http.routed("clips")
    assert clips_call["headers"] == {"Authorization": "Bearer tok-xyz", "Client-Id": "cid"}
    assert clips_call["params"]["game_id"] == "777"
    assert clips_call["params"]["started_at"] == "2026-11-18T11:00:00Z"
    assert clips_call["params"]["ended_at"] == "2026-11-19T11:00:00Z"
    assert clips_call["params"]["first"] == 100


def test_fetch_follows_cursor_until_max_candidates():
    http = FakeHttp()
    http.queues["token"] = [_token()]
    http.queues["games"] = [_games()]
    http.queues["clips"] = [
        _clips(*[_clip_json(f"a{i}", 1000 - i) for i in range(3)], cursor="c1"),
        _clips(*[_clip_json(f"b{i}", 900 - i) for i in range(3)], cursor="c2"),
        _clips(*[_clip_json(f"c{i}", 800 - i) for i in range(3)], cursor="c3"),
    ]

    clips = _source(http, max_candidates=5).fetch(SINCE, UNTIL)

    assert [c.source_clip_id for c in clips] == ["a0", "a1", "a2", "b0", "b1"]
    pages = http.routed("clips")
    assert "after" not in pages[0]["params"]
    assert pages[1]["params"]["after"] == "c1"
    assert len(pages) == 2  # stopped once the cap was reached


def test_fetch_drops_clips_under_min_views_and_stops_paging():
    """Helix returns clips by descending views, so one under the floor ends the scan."""
    http = FakeHttp()
    http.queues["token"] = [_token()]
    http.queues["games"] = [_games()]
    http.queues["clips"] = [
        _clips(_clip_json("hot", 500), _clip_json("cold", 49), cursor="more"),
    ]

    clips = _source(http, min_views=50).fetch(SINCE, UNTIL)

    assert [c.source_clip_id for c in clips] == ["hot"]
    assert len(http.routed("clips")) == 1


def test_fetch_stops_when_pagination_has_no_cursor():
    http = FakeHttp()
    http.queues["token"] = [_token()]
    http.queues["games"] = [_games()]
    http.queues["clips"] = [_clips(_clip_json("only", 100))]

    assert len(_source(http).fetch(SINCE, UNTIL)) == 1


def test_unknown_game_raises_with_its_name():
    http = FakeHttp()
    http.queues["token"] = [_token()]
    http.queues["games"] = [FakeResponse(200, {"data": []})]

    with pytest.raises(TwitchError, match="Grand Theft Auto VI"):
        _source(http).fetch(SINCE, UNTIL)


def test_game_id_and_token_are_reused_across_fetches():
    http = FakeHttp()
    http.queues["token"] = [_token()]
    http.queues["games"] = [_games()]
    http.queues["clips"] = [_clips(), _clips()]

    source = _source(http)
    source.fetch(SINCE, UNTIL)
    source.fetch(SINCE, UNTIL)

    assert len(http.routed("token")) == 1
    assert len(http.routed("games")) == 1


def test_expired_token_is_renewed_before_use():
    http = FakeHttp()
    clock = {"now": UNTIL}
    http.queues["token"] = [_token("first", expires_in=3600), _token("second")]
    http.queues["games"] = [_games()]
    http.queues["clips"] = [_clips(), _clips()]

    source = _source(http, now=lambda: clock["now"])
    source.fetch(SINCE, UNTIL)
    clock["now"] = UNTIL + timedelta(hours=2)
    source.fetch(SINCE, UNTIL)

    assert http.routed("clips")[-1]["headers"]["Authorization"] == "Bearer second"


def test_401_renews_token_once_and_retries():
    http = FakeHttp()
    http.queues["token"] = [_token("stale"), _token("fresh")]
    http.queues["games"] = [_games()]
    http.queues["clips"] = [FakeResponse(401), _clips(_clip_json("x", 100))]

    clips = _source(http).fetch(SINCE, UNTIL)

    assert [c.source_clip_id for c in clips] == ["x"]
    assert http.routed("clips")[-1]["headers"]["Authorization"] == "Bearer fresh"


def test_repeated_401_surfaces_as_twitch_error():
    http = FakeHttp()
    http.queues["token"] = [_token("a"), _token("b")]
    http.queues["games"] = [_games()]
    http.queues["clips"] = [FakeResponse(401), FakeResponse(401)]

    with pytest.raises(TwitchError, match="401"):
        _source(http).fetch(SINCE, UNTIL)


def test_429_waits_until_ratelimit_reset_then_retries():
    http = FakeHttp()
    slept: list[float] = []
    reset_epoch = int(UNTIL.timestamp()) + 7
    http.queues["token"] = [_token()]
    http.queues["games"] = [_games()]
    http.queues["clips"] = [
        FakeResponse(429, headers={"Ratelimit-Reset": str(reset_epoch)}),
        _clips(_clip_json("x", 100)),
    ]

    clips = _source(http, sleep=slept.append).fetch(SINCE, UNTIL)

    assert len(clips) == 1
    assert slept == [7]


def test_bad_credentials_raise_twitch_error():
    http = FakeHttp()
    http.queues["token"] = [FakeResponse(400, {"status": 400, "message": "invalid client"})]

    with pytest.raises(TwitchError, match="token"):
        _source(http).fetch(SINCE, UNTIL)


def test_non_ascii_display_name_gets_no_guessed_channel_url():
    """Helix gives the display name only; a localized one is not the login."""
    http = FakeHttp()
    http.queues["token"] = [_token()]
    http.queues["games"] = [_games()]
    http.queues["clips"] = [_clips(_clip_json("x", 100, broadcaster_name="한국스트리머"))]

    [clip] = _source(http).fetch(SINCE, UNTIL)
    assert clip.broadcaster_url is None
    assert clip.broadcaster_name == "한국스트리머"


def test_from_env_requires_credentials(monkeypatch):
    from app.config import MissingEnvVar

    monkeypatch.delenv("TWITCH_CLIENT_ID", raising=False)
    with pytest.raises(MissingEnvVar, match="TWITCH_CLIENT_ID"):
        TwitchSource.from_env()


# --------------------------------------------------------------------------- #
# upsert
# --------------------------------------------------------------------------- #

@pytest.fixture
def engine(tmp_path):
    eng = get_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(eng)
    return eng


def _candidate(clip_id: str, views: int) -> CandidateClip:
    return CandidateClip(
        source="twitch", source_clip_id=clip_id, url=f"https://clips.twitch.tv/{clip_id}",
        broadcaster_name="alanzoka", broadcaster_url="https://www.twitch.tv/alanzoka",
        title="t", language="pt", view_count=views, duration_s=20.0,
        created_at=datetime(2026, 11, 19, 2, 0, tzinfo=timezone.utc),
    )


def test_upsert_inserts_new_clips(engine):
    with get_session(engine) as session:
        assert upsert_clips(session, [_candidate("a", 10), _candidate("b", 20)]) == (2, 0)
        assert session.query(Clip).count() == 2


def test_upsert_refreshes_views_and_keeps_classification(engine):
    with get_session(engine) as session:
        upsert_clips(session, [_candidate("a", 10)])
        clip = session.query(Clip).one()
        clip.category = "funny"
        clip.classified_at = datetime.now(timezone.utc)
        session.commit()

        assert upsert_clips(session, [_candidate("a", 500)]) == (0, 1)

        clip = session.query(Clip).one()
        assert clip.view_count == 500
        assert clip.category == "funny"
        assert clip.classified_at is not None


def test_upsert_tolerates_duplicates_within_one_batch(engine):
    with get_session(engine) as session:
        assert upsert_clips(session, [_candidate("a", 10), _candidate("a", 30)]) == (1, 0)
        assert session.query(Clip).one().view_count == 30
