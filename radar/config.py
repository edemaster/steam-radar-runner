"""Thresholds and env access in one place so later chunks tune numbers without hunting.

Secrets are read only from environment variables and are never logged.
"""
from __future__ import annotations

import os

USER_AGENT = "steam-radar/0.1 (personal research)"

# A game is "new" and tracked daily for this many days after release.
TRACK_DAYS = 90
# Window (days since release) in which an older game that suddenly grows counts as an "old hit" (chunk 2).
OLD_HIT_MIN_DAYS = 90
OLD_HIT_MAX_DAYS = 455

# Metadata refresh cadence for tracked / coming-soon apps, and the tag-name table.
META_REFRESH_DAYS = 7
TAG_REFRESH_DAYS = 7
# First run sees ~150k apps; cap per-run metadata fetches so it converges over days.
META_MAX_PER_RUN = 20_000
ITEMS_BATCH = 100  # GetItems accepted 200 in testing; 100 leaves headroom.

# Hard ceiling on Steam Web API calls per run (Valve's limit is 100k/day).
STEAM_CALL_LIMIT = 90_000

# Per-host minimum seconds between requests.
HOST_MIN_INTERVAL: dict[str, float] = {
    "api.steampowered.com": 0.25,
    "store.steampowered.com": 1.5,
    "api.twitch.tv": 0.1,
    "id.twitch.tv": 0.1,
}
DEFAULT_MIN_INTERVAL = 0.5

MAX_RETRIES = 5
HTTP_TIMEOUT = 30
TWITCH_CACHE_HOURS = 24  # Twitch terms: no caching beyond 24 h.
TWITCH_MAX_PAGES = 30


def steam_key() -> str | None:
    """Steam Web API key from env, or None (keyless endpoints still work)."""
    return os.environ.get("STEAM_API_KEY") or None


def twitch_credentials() -> tuple[str, str] | None:
    """Twitch client id/secret from env, or None when Twitch is not configured."""
    cid = os.environ.get("TWITCH_CLIENT_ID")
    sec = os.environ.get("TWITCH_CLIENT_SECRET")
    return (cid, sec) if cid and sec else None

# --- Chunk 2: backfill and detection thresholds (tuned only here) ---
HISTORY_MIN_REVIEWS = 50  # below this a game gets one "now" point; no history is worth the calls
HISTORY_OFFSETS = (7, 14, 30, 60, 90, 180, 365)  # days after release
HISTORY_WEEKLY_MAX_AGE = 90  # games this young also get a point every 7 days since release
BACKFILL_CALL_RESERVE = 2_000  # stop this many calls before the Steam ceiling
BACKFILL_COMMIT_EVERY = 50
# Games older than CAND_MAX_DAYS can only matter as old hits (>= OLD_HIT_MIN_REVIEWS). The store's own count
# (GetItems summary, filtered, so a margin) below this means no history calls; measured 2026-11-10: the first
# 5 h backfill spent most of its calls on such games and never reached the old-hit window.
BACKFILL_SKIP_BELOW_STORE_REVIEWS = 1000

# Tiny-team estimate (we cannot see team size; these proxies are the estimate).
TINY_MAX_DEV_APPS = 3
TINY_MAX_PUB_APPS = 10
TINY_MAX_PRICE_CENTS = 2000
MIN_POSITIVE_SHARE = 0.80

OLD_HIT_MIN_REVIEWS = 2000

CAND_MAX_DAYS = 90  # candidate: released at most this many days before the week
CAND_MIN_REVIEWS = 100
CAND_STEADY_MIN_AGE = 14  # from this age the last two 7-day windows are compared
CAND_W1_MIN = 50
CAND_YOUNG_MIN_AGE = 7  # 7..13 days: only first-week reviews count
CAND_YOUNG_FIRST_WEEK = 200
CCU_MIN_POINTS = 10

TAG_TOP_VECTOR = 20
UNUSUAL_TOP_TAGS = 6
UNUSUAL_PAIR_MAX_SHARE = 0.01
SIM_TAG_WEIGHT = 0.7
SIM_DESC_WEIGHT = 0.3
# Calibrated on the first full database (2026-11-11, 34 candidates x 115 old hits): median pair 0.03,
# top 1 % 0.21, top 0.1 % 0.32; real look-alikes (OpenFront / War of Dots, Sort Them Ducks / Supermarket
# Chaos) scored 0.24-0.37 and nothing but a sequel reached the first guess of 0.45. 0.25 is ~ the top 0.5 %.
SIM_MIN = 0.25
MAX_CLUSTER = 8
SHARED_TAG_TOP = 10
SHARED_TAG_FALLBACK = 5
CLONE_TOP = 10

REVIEW_SAMPLE_N = 100
SHORT_PLAY_MINUTES = 30
FEW_GAMES_MAX = 2

# --- Chunk 3: LLM digest (tuned only here) ---
SCOUT_MODEL = os.environ.get("RADAR_SCOUT_MODEL") or "claude-haiku-5-5"
SKEPTIC_MODEL = os.environ.get("RADAR_SKEPTIC_MODEL") or "claude-opus-5-5"
SCOUT_EFFORT = "medium"
SKEPTIC_EFFORT = "high"
SCOUT_MAX_TOKENS = 4000
SKEPTIC_MAX_TOKENS = 16000
# USD per million tokens: (input, output).
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-opus-5-5": (4.00, 20.00),
}
LLM_MONTHLY_CAP_USD = 16.0  # about EUR 15, below the Console limit of $20
LLM_RUN_CAP_USD = 4.0
MAX_SCOUT_CLUSTERS = 10
MAX_SKEPTIC_CARDS = 6
MAX_DIGEST_CONCEPTS = 3
LEDGER_LOOKBACK_WEEKS = 26

# Review-sample red flags (a share at or above the limit; languages at or below it).
RED_FLAG_SHARE_FREE = 0.20
RED_FLAG_SHARE_SHORT_PLAY = 0.50
RED_FLAG_SHARE_FEW_GAMES = 0.50
RED_FLAG_MAX_LANGUAGES = 2
RED_FLAG_LANGUAGES_MIN_N = 50
