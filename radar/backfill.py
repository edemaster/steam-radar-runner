"""One-off history backfill: `python -m radar.backfill --db radar.db [--appids 1,2] [--limit N] [--max-minutes 300]`.

WHY: detection needs review growth curves for games that were released before the daily collector
existed. Cumulative counts at planned dates come from GetAppReviews date ranges (counts only). It is
resumable (a game whose planned points are all stored is skipped), newest release first, and stops
cleanly on a time or call-budget limit so a partial run is still a useful database.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from . import collect, config, db, steam
from .http import CallBudgetExceeded, Client, HttpError

log = logging.getLogger("radar.backfill")


def planned_dates(release: date, today: date) -> list[date]:
    """Days at which a game with enough reviews gets a cumulative point (today always included)."""
    days = {release + timedelta(days=o) for o in config.HISTORY_OFFSETS}
    if (today - release).days <= config.HISTORY_WEEKLY_MAX_AGE:
        days |= {release + timedelta(days=7 * k) for k in range(1, (today - release).days // 7 + 1)}
    return sorted({d for d in days if d <= today} | {today})


def end_of_day_ts(day: date) -> int:
    """Unix time of the last second of the UTC day."""
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()) + 86_399


def history_candidates(con: sqlite3.Connection, today: date, limit: int | None) -> list[tuple[int, date]]:
    """Games released 1..OLD_HIT_MAX_DAYS days ago, newest release first."""
    lo = (today - timedelta(days=config.OLD_HIT_MAX_DAYS)).isoformat()
    hi = (today - timedelta(days=1)).isoformat()
    rows = con.execute("""SELECT appid, release_date FROM apps
                          WHERE type='game' AND coming_soon=0 AND release_date BETWEEN ? AND ?
                          ORDER BY release_date DESC, appid DESC""", (lo, hi)).fetchall()
    out = [(r[0], date.fromisoformat(r[1])) for r in rows]
    return out[:limit] if limit else out


def _put(con: sqlite3.Connection, appid: int, day: date, total: int, pos: int) -> None:
    con.execute("INSERT OR REPLACE INTO review_points VALUES(?,?,?,?)", (appid, day.isoformat(), total, pos))


def backfill_app(con: sqlite3.Connection, client: Client, appid: int, release: date, today: date) -> bool:
    """Store one game's points; returns True when it got a full history (False: skipped or thin).

    Raises CallBudgetExceeded/HttpError after storing whatever points were complete.
    """
    have = {r[0]: (r[1], r[2]) for r in con.execute(
        "SELECT upto, reviews_total, reviews_positive FROM review_points WHERE appid=?", (appid,))}
    now = have.get(today.isoformat())
    if now is None:
        now = steam.review_count(client, appid)
        _put(con, appid, today, *now)
    if now[0] < config.HISTORY_MIN_REVIEWS:
        return False
    for day in planned_dates(release, today):
        if day.isoformat() in have or day == today:
            continue
        total, pos = steam.review_count(client, appid, start_ts=1, end_ts=end_of_day_ts(day))
        _put(con, appid, day, total, pos)
    return True


def refresh_store_reviews(con: sqlite3.Connection, client: Client, appids: list[int], today: date,
                          st: collect.Stats) -> None:
    """Fill apps.store_reviews for old games that have neither it nor any review point (100 per call)."""
    if not appids:
        return
    have_pts = {r[0] for r in con.execute("SELECT DISTINCT appid FROM review_points")}
    known = {r[0] for r in con.execute("SELECT appid FROM apps WHERE store_reviews IS NOT NULL")}
    need = [a for a in appids if a not in have_pts and a not in known]
    try:
        for batch in steam.chunks(need):
            for item in steam.get_items(client, batch):
                if item is not None:
                    collect.store_meta(con, item, today)
            con.commit()
    except CallBudgetExceeded:
        st.budget_hit = True
    log.info("store review counts refreshed for %d old games", len(need))


def run(db_path: str, seed: list[int] | None = None, limit: int | None = None, max_minutes: float = 300,
        client: Client | None = None, today: date | None = None,
        clock: Callable[[], float] = time.monotonic) -> int:
    """Backfill metadata and review history; returns the exit code."""
    today = today or datetime.now(timezone.utc).date()
    client = client or Client()
    con = db.connect(db_path)
    st = collect.Stats()
    t0 = clock()
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    key = config.steam_key()
    if not key:
        log.warning("STEAM_API_KEY not set: no GetAppList; using --appids and existing rows only")
    collect.refresh_tags(con, client, today, st)
    collect.discover(con, client, key, seed or [], today, st)

    ids = [r[0] for r in con.execute("SELECT appid FROM apps WHERE meta_updated IS NULL ORDER BY appid DESC")]
    ids = ids[:limit] if limit else ids
    try:
        for batch in steam.chunks(ids):
            for appid, item in zip(batch, steam.get_items(client, batch)):
                if item is None:
                    st.errors += 1
                    continue
                collect.store_meta(con, item, today)
                st.meta += 1
            con.commit()
    except CallBudgetExceeded:
        st.budget_hit = True
    collect.recompute_dev_counts(con)

    todo = history_candidates(con, today, limit)
    old_from = today - timedelta(days=config.CAND_MAX_DAYS)
    if not st.budget_hit:
        refresh_store_reviews(con, client, [a for a, rel in todo if rel < old_from], today, st)
    small = {r[0] for r in con.execute("SELECT appid FROM apps WHERE store_reviews < ?",
                                       (config.BACKFILL_SKIP_BELOW_STORE_REVIEWS,))}
    skipped = 0
    scanned = full = 0
    remaining = len(todo)
    ceiling = getattr(client, "steam_call_limit", config.STEAM_CALL_LIMIT) - config.BACKFILL_CALL_RESERVE
    stop = ""
    for appid, release in todo:
        if st.budget_hit or client.steam_calls >= ceiling:
            stop = "call budget"
            break
        if (clock() - t0) / 60 >= max_minutes:
            stop = "time limit"
            break
        if release < old_from and appid in small:
            skipped += 1
            remaining -= 1
            continue
        try:
            if backfill_app(con, client, appid, release, today):
                full += 1
        except CallBudgetExceeded:
            stop = "call budget"
            break
        except HttpError as exc:
            st.errors += 1
            log.error("history failed for %d: %s", appid, exc)
            continue
        scanned += 1
        remaining -= 1
        if scanned % config.BACKFILL_COMMIT_EVERY == 0:
            con.commit()
    con.commit()
    minutes = (clock() - t0) / 60
    con.execute("INSERT INTO runs(started, finished, kind, steam_calls, errors, note) VALUES(?,?,?,?,?,?)",
                (started, datetime.now(timezone.utc).isoformat(timespec="seconds"), "backfill",
                 client.steam_calls, st.errors, f"stopped: {stop}" if stop else ""))
    con.commit()
    con.close()
    print(f"backfill: apps_scanned={scanned} apps_with_history={full} steam_calls={client.steam_calls} "
          f"minutes={minutes:.1f} skipped_small_old={skipped} remaining={remaining}" + (f" stopped={stop}" if stop else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="radar.db")
    ap.add_argument("--appids", default="", help="comma-separated appids to seed (testing)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--max-minutes", type=float, default=300)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    seed = [int(x) for x in args.appids.split(",") if x.strip()]
    return run(args.db, seed, args.limit, args.max_minutes)


if __name__ == "__main__":
    sys.exit(main())
