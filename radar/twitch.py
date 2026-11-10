"""Twitch Helix wrappers and the 24 h purge.

WHY: Twitch's developer agreement forbids caching its data beyond 24 hours, so numbers live only in
twitch_live, are deleted on every snapshot run, and are never copied to another table.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from . import config
from .http import Client, HttpError

log = logging.getLogger(__name__)

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
HELIX = "https://api.twitch.tv/helix"
RELOOKUP_DAYS = 30  # retry unmatched names occasionally; a game may appear on Twitch later


class Twitch:
    """Client-credentials Helix client."""

    def __init__(self, client: Client, client_id: str, client_secret: str) -> None:
        self.client = client
        self.client_id = client_id
        self._secret = client_secret
        self._token: str | None = None

    def _headers(self) -> dict[str, str]:
        if self._token is None:
            resp = self.client.post_json(TOKEN_URL, data={
                "client_id": self.client_id, "client_secret": self._secret,
                "grant_type": "client_credentials"})
            self._token = resp["access_token"]
        return {"Client-Id": self.client_id, "Authorization": f"Bearer {self._token}"}

    def find_game(self, name: str) -> tuple[str | None, str]:
        """(twitch game id, match method) by exact (case-insensitive) name; (None, 'none') if no match."""
        resp = self.client.get_json(f"{HELIX}/games", params={"name": name}, headers=self._headers())
        for g in resp.get("data", []):
            if g.get("name", "").casefold() == name.casefold():
                return str(g["id"]), "exact_name"
        return None, "none"

    def live_totals(self, game_id: str) -> tuple[int, int]:
        """(channels, viewers) currently streaming the game, summed across pages."""
        channels = viewers = 0
        cursor: str | None = None
        for _ in range(config.TWITCH_MAX_PAGES):
            params: dict[str, str | int] = {"game_id": game_id, "first": 100}
            if cursor:
                params["after"] = cursor
            resp = self.client.get_json(f"{HELIX}/streams", params=params, headers=self._headers())
            data = resp.get("data", [])
            channels += len(data)
            viewers += sum(int(s.get("viewer_count", 0)) for s in data)
            cursor = (resp.get("pagination") or {}).get("cursor")
            if not cursor or not data:
                break
        return channels, viewers


def purge_old(con: sqlite3.Connection, now: datetime | None = None) -> int:
    """Delete twitch_live rows older than 24 h; returns rows removed."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=config.TWITCH_CACHE_HOURS)).isoformat(timespec="seconds")
    cur = con.execute("DELETE FROM twitch_live WHERE fetched_at < ?", (cutoff,))
    con.commit()
    return cur.rowcount


def snapshot(con: sqlite3.Connection, tw: Twitch, appids: list[int],
             now: datetime | None = None) -> tuple[int, int]:
    """Purge, then record live channels/viewers for appids. Returns (rows written, errors)."""
    now = now or datetime.now(timezone.utc)
    purge_old(con, now)
    written = errors = 0
    today = now.date().isoformat()
    for appid in appids:
        try:
            row = con.execute("SELECT twitch_game_id, match_method, looked_up FROM twitch_games WHERE appid=?",
                              (appid,)).fetchone()
            stale = row is not None and row["twitch_game_id"] is None and \
                (now.date() - datetime.fromisoformat(row["looked_up"]).date()).days >= RELOOKUP_DAYS
            if row is None or stale:
                app = con.execute("SELECT name FROM apps WHERE appid=?", (appid,)).fetchone()
                if app is None or not app["name"]:
                    continue
                gid, method = tw.find_game(app["name"])
                con.execute("INSERT OR REPLACE INTO twitch_games VALUES(?,?,?,?)", (appid, gid, method, today))
            else:
                gid = row["twitch_game_id"]
            if gid is None:
                continue
            channels, viewers = tw.live_totals(gid)
            con.execute("INSERT OR REPLACE INTO twitch_live VALUES(?,?,?,?,?)",
                        (appid, gid, now.isoformat(timespec="seconds"), channels, viewers))
            written += 1
        except HttpError as exc:
            errors += 1
            log.error("Twitch snapshot failed for %d: %s", appid, exc)
    con.commit()
    return written, errors
