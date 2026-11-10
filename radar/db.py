"""SQLite schema with idempotent migrations keyed on PRAGMA user_version.

WHY: the DB is a long-lived release asset; later chunks add migrations without breaking old files.
No reviewer identities or other per-person data are ever stored, only counts.
"""
from __future__ import annotations

import sqlite3

MIGRATIONS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS apps(
        appid INTEGER PRIMARY KEY, name TEXT, type TEXT, first_seen DATE, release_date DATE,
        coming_soon INTEGER DEFAULT 0, developers TEXT, publishers TEXT, self_published INTEGER,
        dev_app_count INTEGER, price_cents INTEGER, is_free INTEGER, short_desc TEXT, meta_updated DATE);
    CREATE TABLE IF NOT EXISTS tags(tagid INTEGER PRIMARY KEY, name TEXT);
    CREATE TABLE IF NOT EXISTS app_tags(
        appid INTEGER, tagid INTEGER, weight INTEGER, PRIMARY KEY(appid, tagid));
    CREATE TABLE IF NOT EXISTS daily(
        appid INTEGER, day DATE, ccu INTEGER, reviews_total INTEGER, reviews_positive INTEGER,
        PRIMARY KEY(appid, day));
    CREATE TABLE IF NOT EXISTS review_points(
        appid INTEGER, upto DATE, reviews_total INTEGER, reviews_positive INTEGER,
        PRIMARY KEY(appid, upto));
    CREATE TABLE IF NOT EXISTS watch(appid INTEGER PRIMARY KEY, reason TEXT);
    CREATE TABLE IF NOT EXISTS twitch_games(
        appid INTEGER PRIMARY KEY, twitch_game_id TEXT, match_method TEXT, looked_up DATE);
    CREATE TABLE IF NOT EXISTS twitch_live(
        appid INTEGER, twitch_game_id TEXT, fetched_at TEXT, channels INTEGER, viewers INTEGER,
        PRIMARY KEY(appid, fetched_at));
    CREATE TABLE IF NOT EXISTS runs(
        id INTEGER PRIMARY KEY, started TEXT, finished TEXT, kind TEXT, steam_calls INTEGER,
        errors INTEGER, note TEXT);
    CREATE TABLE IF NOT EXISTS spend(
        id INTEGER PRIMARY KEY, ts TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER,
        usd REAL, purpose TEXT);
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
    CREATE INDEX IF NOT EXISTS idx_apps_release ON apps(release_date);
    """,
    # v2 (chunk 2): publisher counts, detected signals, aggregate-only review samples.
    """
    ALTER TABLE apps ADD COLUMN pub_app_count INT;
    CREATE INDEX IF NOT EXISTS idx_app_tags_tag ON app_tags(tagid);
    CREATE TABLE IF NOT EXISTS signals(
        week DATE, fingerprint TEXT, appids JSON, roles JSON, shared_tags JSON, score REAL, details JSON,
        PRIMARY KEY(week, fingerprint));
    CREATE TABLE IF NOT EXISTS review_samples(
        appid INTEGER, day DATE, n INT, share_short_play REAL, share_few_games REAL, share_free REAL,
        languages INT, PRIMARY KEY(appid, day));
    """,
    # v3: the store's own review count, so the backfill can skip old games that can never be old hits.
    """
    ALTER TABLE apps ADD COLUMN store_reviews INT;
    """,
]


def connect(path: str) -> sqlite3.Connection:
    """Open (creating if needed) and migrate the database."""
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    migrate(con)
    return con


def migrate(con: sqlite3.Connection) -> None:
    """Apply pending migrations; safe to call repeatedly."""
    version = con.execute("PRAGMA user_version").fetchone()[0]
    for i in range(version, len(MIGRATIONS)):
        con.executescript(MIGRATIONS[i])
        con.execute(f"PRAGMA user_version = {i + 1}")
    con.commit()
