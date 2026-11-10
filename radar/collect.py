"""Daily collector: `python -m radar.collect [--db radar.db] [--limit N] [--appids 1,2]`.

WHY: gathers raw numbers only (no LLM). A failing endpoint is logged and skipped so the run keeps
what it has; the exit code is non-zero only when nothing at all could be collected.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import config, db, steam
from .http import CallBudgetExceeded, Client, HttpError

log = logging.getLogger("radar.collect")


class Stats:
    """Counters for the summary line and the runs table."""

    def __init__(self) -> None:
        self.new_apps = 0
        self.meta = 0
        self.daily = 0
        self.errors = 0
        self.budget_hit = False


def refresh_tags(con: sqlite3.Connection, client: Client, today: date, st: Stats) -> None:
    """Reload tag names when the table is empty or older than a week."""
    row = con.execute("SELECT value FROM meta WHERE key='tags_updated'").fetchone()
    empty = con.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 0
    if not empty and row and (today - date.fromisoformat(row[0])).days < config.TAG_REFRESH_DAYS:
        return
    try:
        tags = steam.tag_list(client)
    except HttpError as exc:
        st.errors += 1
        log.error("GetTagList failed: %s", exc)
        return
    con.executemany("INSERT OR REPLACE INTO tags VALUES(?,?)", tags.items())
    con.execute("INSERT OR REPLACE INTO meta VALUES('tags_updated', ?)", (today.isoformat(),))
    con.commit()


def discover(con: sqlite3.Connection, client: Client, key: str | None, seed: list[int],
             today: date, st: Stats) -> None:
    """Insert unknown appids (GetAppList needs a key; without one only --appids seeds are used)."""
    rows: list[tuple[int, str, str]] = [(a, "", today.isoformat()) for a in seed]
    if key:
        try:
            rows += [(int(a["appid"]), a.get("name", ""), today.isoformat())
                     for a in steam.app_list(client, key)]
        except HttpError as exc:
            st.errors += 1
            log.error("GetAppList failed: %s", exc)
    elif not seed:
        log.warning("STEAM_API_KEY not set: skipping GetAppList (use --appids to seed)")
    before = con.total_changes
    con.executemany("INSERT OR IGNORE INTO apps(appid, name, first_seen) VALUES(?,?,?)",
                    [(a, n, d) for a, n, d in rows])
    st.new_apps = con.total_changes - before
    con.commit()


def meta_candidates(con: sqlite3.Connection, today: date, cap: int) -> list[int]:
    """Appids needing metadata: never fetched, or recent/coming-soon and stale; newest appid first."""
    track_from = (today - timedelta(days=config.TRACK_DAYS)).isoformat()
    stale = (today - timedelta(days=config.META_REFRESH_DAYS)).isoformat()
    rows = con.execute(
        """SELECT appid FROM apps
           WHERE meta_updated IS NULL
              OR (type = 'game' AND meta_updated <= ? AND (coming_soon = 1 OR release_date >= ?))
           ORDER BY appid DESC LIMIT ?""", (stale, track_from, cap)).fetchall()
    return [r[0] for r in rows]


def store_meta(con: sqlite3.Connection, item: dict[str, Any], today: date) -> None:
    """Write one parsed item into apps / app_tags."""
    if item.get("unavailable"):
        con.execute("UPDATE apps SET type='unavailable', meta_updated=? WHERE appid=?",
                    (today.isoformat(), item["appid"]))
        return
    con.execute(
        """UPDATE apps SET name=?, type=?, release_date=?, coming_soon=?, developers=?, publishers=?,
           self_published=?, price_cents=?, is_free=?, short_desc=?, meta_updated=? WHERE appid=?""",
        (item["name"], item["type"], item["release_date"], item["coming_soon"],
         json.dumps(item["developers"]), json.dumps(item["publishers"]), item["self_published"],
         item["price_cents"], item["is_free"], item["short_desc"], today.isoformat(), item["appid"]))
    con.execute("UPDATE apps SET store_reviews=? WHERE appid=?",
                ((item.get("review_summary") or {}).get("count"), item["appid"]))
    con.execute("DELETE FROM app_tags WHERE appid=?", (item["appid"],))
    con.executemany("INSERT INTO app_tags VALUES(?,?,?)",
                    [(item["appid"], t, w) for t, w in item["tags"]])


def refresh_meta(con: sqlite3.Connection, client: Client, today: date, limit: int | None,
                 st: Stats) -> None:
    """Fetch metadata in batches, capped per run so the first run converges over several days."""
    cap = min(config.META_MAX_PER_RUN, limit) if limit else config.META_MAX_PER_RUN
    ids = meta_candidates(con, today, cap)
    done = 0
    for batch in steam.chunks(ids):
        try:
            items = steam.get_items(client, batch)
        except CallBudgetExceeded:
            st.budget_hit = True
            return
        for appid, item in zip(batch, items):
            if item is None:
                st.errors += 1
                continue
            store_meta(con, item, today)
            st.meta += 1
        con.commit()
        done += len(batch)
        if done % 2000 == 0 or done == len(ids):
            log.info("meta %d/%d", done, len(ids))  # progress, so a long first run is not silent
    recompute_dev_counts(con)


def recompute_dev_counts(con: sqlite3.Connection) -> None:
    """dev_app_count / pub_app_count = apps sharing any developer / publisher name (indexed temp tables;
    json_each per app is too slow)."""
    con.execute("DROP TABLE IF EXISTS temp.dev_apps")
    con.execute("CREATE TEMP TABLE dev_apps(name TEXT, appid INTEGER)")
    con.execute("INSERT INTO temp.dev_apps SELECT d.value, a.appid FROM apps a, json_each(a.developers) d "
                "WHERE a.developers IS NOT NULL")
    con.execute("CREATE INDEX temp.dev_apps_name ON dev_apps(name)")
    con.execute("""UPDATE apps SET dev_app_count = (
                       SELECT COUNT(DISTINCT appid) FROM temp.dev_apps
                       WHERE name IN (SELECT value FROM json_each(apps.developers)))
                   WHERE developers IS NOT NULL""")
    con.execute("DROP TABLE temp.dev_apps")
    con.execute("DROP TABLE IF EXISTS temp.pub_apps")
    con.execute("CREATE TEMP TABLE pub_apps(name TEXT, appid INTEGER)")
    con.execute("INSERT INTO temp.pub_apps SELECT d.value, a.appid FROM apps a, json_each(a.publishers) d "
                "WHERE a.publishers IS NOT NULL")
    con.execute("CREATE INDEX temp.pub_apps_name ON pub_apps(name)")
    con.execute("""UPDATE apps SET pub_app_count = (
                       SELECT COUNT(DISTINCT appid) FROM temp.pub_apps
                       WHERE name IN (SELECT value FROM json_each(apps.publishers)))
                   WHERE publishers IS NOT NULL""")
    con.execute("DROP TABLE temp.pub_apps")
    con.commit()


def tracked_appids(con: sqlite3.Connection, today: date, limit: int | None) -> list[int]:
    """Released games within TRACK_DAYS plus the watch list."""
    since = (today - timedelta(days=config.TRACK_DAYS)).isoformat()
    rows = con.execute(
        """SELECT appid FROM apps WHERE type='game' AND release_date >= ? AND release_date <= ?
           UNION SELECT appid FROM watch ORDER BY appid DESC""", (since, today.isoformat())).fetchall()
    ids = [r[0] for r in rows]
    return ids[:limit] if limit else ids


def collect_daily(con: sqlite3.Connection, client: Client, ids: list[int], today: date,
                  st: Stats) -> None:
    """Upsert today's ccu and review totals; one failing endpoint leaves its column NULL."""
    for appid in ids:
        ccu = total = pos = None
        try:
            ccu = steam.current_players(client, appid)
        except CallBudgetExceeded:
            st.budget_hit = True
            return
        except HttpError as exc:
            st.errors += 1
            log.error("ccu failed for %d: %s", appid, exc)
        try:
            total, pos = steam.review_count(client, appid)
        except CallBudgetExceeded:
            st.budget_hit = True
            return
        except HttpError as exc:
            st.errors += 1
            log.error("reviews failed for %d: %s", appid, exc)
        if ccu is None and total is None:
            continue
        con.execute(
            """INSERT INTO daily(appid, day, ccu, reviews_total, reviews_positive) VALUES(?,?,?,?,?)
               ON CONFLICT(appid, day) DO UPDATE SET ccu=COALESCE(excluded.ccu, ccu),
                 reviews_total=COALESCE(excluded.reviews_total, reviews_total),
                 reviews_positive=COALESCE(excluded.reviews_positive, reviews_positive)""",
            (appid, today.isoformat(), ccu, total, pos))
        st.daily += 1
        if st.daily % 200 == 0:
            con.commit()
            log.info("daily %d/%d", st.daily, len(ids))
    con.commit()


def run(db_path: str, limit: int | None = None, seed: list[int] | None = None,
        client: Client | None = None, today: date | None = None) -> int:
    """Run one collection; returns the process exit code."""
    today = today or datetime.now(timezone.utc).date()
    client = client or Client()
    con = db.connect(db_path)
    st = Stats()
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    key = config.steam_key()
    refresh_tags(con, client, today, st)
    discover(con, client, key, seed or [], today, st)
    refresh_meta(con, client, today, limit, st)
    if not st.budget_hit:
        collect_daily(con, client, tracked_appids(con, today, limit), today, st)
    note = "call budget hit" if st.budget_hit else ""
    con.execute("INSERT INTO runs(started, finished, kind, steam_calls, errors, note) VALUES(?,?,?,?,?,?)",
                (started, datetime.now(timezone.utc).isoformat(timespec="seconds"), "daily",
                 client.steam_calls, st.errors, note))
    con.commit()
    con.close()
    log.info("run done: new_apps=%d meta=%d daily=%d steam_calls=%d errors=%d %s",
             st.new_apps, st.meta, st.daily, client.steam_calls, st.errors, note)
    return 1 if (st.meta + st.daily + st.new_apps == 0 and st.errors > 0) else 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="radar.db")
    ap.add_argument("--limit", type=int, default=None, help="cap metadata and daily fetches (testing)")
    ap.add_argument("--appids", default="", help="comma-separated appids to seed (testing)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    seed = [int(x) for x in args.appids.split(",") if x.strip()]
    return run(args.db, args.limit, seed)


if __name__ == "__main__":
    sys.exit(main())
