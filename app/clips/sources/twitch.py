"""Twitch Helix clip source (app access token; no user login).

Endpoints: POST id.twitch.tv/oauth2/token (client credentials),
GET helix/games?name=, GET helix/clips?game_id=&started_at=&ended_at=.
Helix returns clips by descending view count, which is what lets `fetch`
stop paging at the first clip under `min_views`.
"""
from __future__ import annotations

import time as _time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import requests

from app import config
from app.clips.models import CandidateClip

_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
_API = "https://api.twitch.tv/helix"
_PAGE_SIZE = 100  # Helix maximum for `first`
_TOKEN_MARGIN = timedelta(seconds=60)
_MAX_ATTEMPTS = 3
_LOG_PREFIX = "[twitch]"


class TwitchError(RuntimeError):
    """A Helix call failed in a way retrying will not fix."""


def _rfc3339(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _channel_url(display_name: str) -> str | None:
    # Helix clips carry the display name, not the login. For ASCII names the
    # login is the lowercased display name; localized names have no such rule.
    if display_name.isascii() and display_name.replace("_", "").isalnum():
        return f"https://www.twitch.tv/{display_name.lower()}"
    return None


class TwitchSource:
    name = "twitch"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        game_name: str,
        min_views: int,
        max_candidates: int,
        http: Any = None,
        sleep: Callable[[float], None] = _time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._game_name = game_name
        self._min_views = min_views
        self._max_candidates = max_candidates
        self._http = http or requests.Session()
        self._sleep = sleep
        self._now = now
        self._token: str | None = None
        self._token_expires: datetime | None = None
        self._game_id: str | None = None

    @classmethod
    def from_env(cls) -> "TwitchSource":
        return cls(
            client_id=config.require("TWITCH_CLIENT_ID"),
            client_secret=config.require("TWITCH_CLIENT_SECRET"),
            game_name=config.CLIPS_GAME_NAME,
            min_views=config.CLIPS_MIN_VIEWS,
            max_candidates=config.CLIPS_MAX_CANDIDATES,
        )

    # ------------------------------------------------------------------ auth

    def _fetch_token(self) -> None:
        resp = self._http.post(
            _TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "client_credentials",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise TwitchError(
                f"token request failed with {resp.status_code}: {resp.json()!r}. "
                f"Check TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET."
            )
        body = resp.json()
        self._token = body["access_token"]
        self._token_expires = self._now() + timedelta(seconds=int(body["expires_in"]))

    def _ensure_token(self) -> str:
        if self._token is None or self._now() >= self._token_expires - _TOKEN_MARGIN:
            self._fetch_token()
        return self._token

    # --------------------------------------------------------------- request

    def _get(self, path: str, params: dict[str, Any]) -> dict:
        """GET a Helix endpoint. Renews the token once on 401, waits out 429
        until `Ratelimit-Reset`, backs off on 5xx/network errors."""
        renewed = False
        attempt = 0
        while True:
            headers = {"Authorization": f"Bearer {self._ensure_token()}", "Client-Id": self._client_id}
            try:
                resp = self._http.get(f"{_API}/{path}", params=params, headers=headers, timeout=30)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                attempt += 1
                if attempt >= _MAX_ATTEMPTS:
                    raise TwitchError(f"GET {path} failed after {attempt} attempts: {exc}") from exc
                self._sleep(2 ** attempt)
                continue

            status = resp.status_code
            if status == 200:
                return resp.json()
            if status == 401 and not renewed:
                renewed = True
                self._token = None
                continue
            if status == 429 or status >= 500:
                attempt += 1
                if attempt >= _MAX_ATTEMPTS:
                    raise TwitchError(f"GET {path} returned {status} after {attempt} attempts")
                self._sleep(self._retry_delay(resp, attempt))
                continue
            raise TwitchError(f"GET {path} returned {status}: {resp.json()!r}")

    def _retry_delay(self, resp: Any, attempt: int) -> float:
        reset = resp.headers.get("Ratelimit-Reset")
        if resp.status_code == 429 and reset:
            return max(0.0, float(reset) - self._now().timestamp())
        return float(2 ** attempt)

    # ------------------------------------------------------------------ data

    def _resolve_game_id(self) -> str:
        if self._game_id is None:
            data = self._get("games", {"name": self._game_name}).get("data", [])
            if not data:
                raise TwitchError(f"game not found on Twitch: {self._game_name!r}")
            self._game_id = data[0]["id"]
        return self._game_id

    def fetch(self, since: datetime, until: datetime) -> list[CandidateClip]:
        game_id = self._resolve_game_id()
        params: dict[str, Any] = {
            "game_id": game_id,
            "started_at": _rfc3339(since),
            "ended_at": _rfc3339(until),
            "first": _PAGE_SIZE,
        }
        out: list[CandidateClip] = []
        while True:
            body = self._get("clips", params)
            for raw in body.get("data", []):
                if raw["view_count"] < self._min_views:
                    print(f"{_LOG_PREFIX} {len(out)} clips at or above {self._min_views} views")
                    return out
                out.append(self._to_candidate(raw))
                if len(out) >= self._max_candidates:
                    print(f"{_LOG_PREFIX} reached cap of {self._max_candidates} clips")
                    return out
            cursor = body.get("pagination", {}).get("cursor")
            if not cursor:
                print(f"{_LOG_PREFIX} {len(out)} clips in window")
                return out
            params = {**params, "after": cursor}

    @staticmethod
    def _to_candidate(raw: dict) -> CandidateClip:
        return CandidateClip(
            source="twitch",
            source_clip_id=raw["id"],
            url=raw["url"],
            broadcaster_name=raw["broadcaster_name"],
            broadcaster_url=_channel_url(raw["broadcaster_name"]),
            title=raw.get("title", ""),
            language=raw.get("language") or None,
            view_count=raw["view_count"],
            duration_s=float(raw["duration"]),
            created_at=datetime.fromisoformat(raw["created_at"].replace("Z", "+00:00")),
        )
