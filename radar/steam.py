"""Steam Web API wrappers (verified live 2026-11-10 unless noted).

WHY: every endpoint's quirks live here so collect.py stays declarative. Only counts are
returned for reviews; reviewer identities in responses are discarded unread.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Iterator

from . import config
from .http import Client, HttpError

log = logging.getLogger(__name__)

API = "https://api.steampowered.com"
ITEM_TYPE_GAME = 0  # GetItems `type` is an integer; 0 == game (verified: CS2, Stardew Valley).


def app_list(client: Client, key: str, max_results: int = 50_000) -> Iterator[dict[str, Any]]:
    """Yield every game {appid, name} via IStoreService/GetAppList, paging on last_appid. Needs a key."""
    last = 0
    while True:
        params: dict[str, Any] = {
            "key": key, "include_games": "true", "include_dlc": "false",
            "include_software": "false", "include_videos": "false", "include_hardware": "false",
            "max_results": max_results, "last_appid": last,
        }
        resp = client.get_json(f"{API}/IStoreService/GetAppList/v1/", params=params).get("response", {})
        apps = resp.get("apps", [])
        yield from apps
        if not resp.get("have_more_results") or not apps:
            return
        last = int(resp.get("last_appid") or apps[-1]["appid"])


def tag_list(client: Client) -> dict[int, str]:
    """Tag id -> English name (works keyless)."""
    resp = client.get_json(f"{API}/IStoreService/GetTagList/v1/", params={"language": "english"})
    return {int(t["tagid"]): t["name"] for t in resp.get("response", {}).get("tags", [])}


def current_players(client: Client, appid: int) -> int | None:
    """Live concurrent players (keyless); None when Steam has no figure.

    WHY: unreleased/hidden apps answer HTTP 404 (seen live for a coming-soon appid): that is "no data",
    not an error worth counting.
    """
    try:
        resp = client.get_json(f"{API}/ISteamUserStats/GetNumberOfCurrentPlayers/v1/",
                               params={"appid": appid}).get("response", {})
    except HttpError as exc:
        if exc.status == 404:
            return None
        raise
    return resp.get("player_count") if resp.get("result") == 1 else None


def review_count(client: Client, appid: int, start_ts: int | None = None,
                 end_ts: int | None = None) -> tuple[int, int]:
    """(total, positive) reviews of all languages/purchase types, optionally within [start_ts, end_ts].

    WHY: IUserReviewsService/GetAppReviews returns query_summary totals for the whole query, so one
    call with num_per_page=1 (0 means the default 20) and day_range=0 (else the window is 30 days)
    gives exact counts without paging. Keyless. The date range is ignored unless both ends are set.
    """
    params: dict[str, Any] = {
        "appid": appid, "filter": 1, "day_range": 0, "languages[0]": "all",
        "purchase_type": 1, "num_per_page": 1,
    }
    if start_ts is not None and end_ts is not None:
        params["date_range_start"] = int(start_ts)
        params["date_range_end"] = int(end_ts)
    resp = client.get_json(f"{API}/IUserReviewsService/GetAppReviews/v1/", params=params).get("response", {})
    summary = resp.get("query_summary", {})
    if "total_reviews" in summary:
        return int(summary["total_reviews"]), int(summary.get("total_positive", 0))
    # No reviews: Steam omits the score fields on empty queries.
    return int(resp.get("total_matching", 0)), 0


def _day(ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).date().isoformat() if ts else None


def _price(item: dict[str, Any]) -> tuple[int | None, int | None]:
    """(final price in cents, discount percent) from best_purchase_option; None if absent."""
    opt = item.get("best_purchase_option") or {}
    cents = opt.get("final_price_in_cents")
    return (int(cents) if cents is not None else None, opt.get("discount_pct"))


def parse_item(item: dict[str, Any]) -> dict[str, Any]:
    """Normalise one GetItems store_item; {'appid', 'unavailable': True} when success != 1."""
    if item.get("success") != 1:
        return {"appid": int(item.get("id", 0)), "unavailable": True}
    basic = item.get("basic_info", {})
    rel = item.get("release", {})
    devs = [d["name"] for d in basic.get("developers", [])]
    pubs = [p["name"] for p in basic.get("publishers", [])]
    cents, discount = _price(item)
    free = bool(item.get("is_free"))
    reviews = (item.get("reviews") or {}).get("summary_filtered", {})
    return {
        "appid": int(item["id"]),
        "name": item.get("name", ""),
        "type": "game" if item.get("type") == ITEM_TYPE_GAME else f"type_{item.get('type')}",
        "release_date": _day(rel.get("steam_release_date")),
        "coming_soon": 1 if (item.get("is_coming_soon") or rel.get("is_coming_soon")) else 0,
        "developers": devs, "publishers": pubs,
        "self_published": 1 if devs and set(devs) == set(pubs) else 0,
        "price_cents": 0 if free else cents, "discount_pct": discount, "is_free": 1 if free else 0,
        "short_desc": basic.get("short_description", ""),
        "tags": [(int(t["tagid"]), int(t.get("weight", 0))) for t in item.get("tags", [])],
        "review_summary": ({"count": reviews.get("review_count"), "pct": reviews.get("percent_positive")}
                           if reviews else None),
    }


def get_items(client: Client, appids: list[int]) -> list[dict[str, Any] | None]:
    """Metadata for appids via IStoreBrowseService/GetItems (keyless); result order matches input.

    A failing batch is split in halves so one bad id cannot sink 100 others; a single failing id
    (or one missing from the response) yields None and a logged error. Unavailable ids are
    returned as {'unavailable': True} so callers can stop retrying them.
    """
    if not appids:
        return []
    req = {
        "ids": [{"appid": a} for a in appids],
        "context": {"language": "english", "country_code": "US"},
        "data_request": {"include_basic_info": True, "include_tag_count": 20, "include_release": True,
                         "include_all_purchase_options": True, "include_reviews": True},
    }
    try:
        resp = client.get_json(f"{API}/IStoreBrowseService/GetItems/v1/",
                               params={"input_json": json.dumps(req, separators=(",", ":"))})
    except HttpError as exc:
        if len(appids) == 1:
            log.error("GetItems failed for %d: %s", appids[0], exc)
            return [None]
        mid = len(appids) // 2
        return get_items(client, appids[:mid]) + get_items(client, appids[mid:])
    by_id = {int(it.get("id", 0)): parse_item(it) for it in resp.get("response", {}).get("store_items", [])}
    return [by_id.get(a) for a in appids]


def chunks(seq: list[int], n: int = config.ITEMS_BATCH) -> Iterator[list[int]]:
    """Split into batches of n."""
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def review_sample(client: Client, appid: int,
                  n: int = config.REVIEW_SAMPLE_N) -> dict[str, Any] | None:
    """Aggregates over the n most recent reviews (all languages); None when there are none.

    WHY: a bot/shill check needs only shares (short playtime, few owned games, free copies, language
    spread). filter=1 is newest-first (verified live 2026-11-10; the string "recent" is ignored and
    returns "helpful" order). author.num_games_owned is absent from the live response (only
    num_reviews exists), so share_few_games is None unless a row carries it. The response holds reviewer ids and text; only those four fields are read, counted,
    and the response is dropped, so nothing per-person is ever kept. One call (num_per_page max 100).
    """
    params: dict[str, Any] = {
        "appid": appid, "filter": 1, "day_range": 0, "languages[0]": "all",
        "purchase_type": 1, "num_per_page": min(n, 100), "cursor": "*",
    }
    resp = client.get_json(f"{API}/IUserReviewsService/GetAppReviews/v1/", params=params).get("response", {})
    rows = resp.get("reviews") or []
    if not rows:
        return None
    short = few = free = owned_rows = 0
    langs: set[str] = set()
    for r in rows:
        author = r.get("author") or {}
        if int(author.get("playtime_forever", 0)) < config.SHORT_PLAY_MINUTES:
            short += 1
        # "Thin account": few owned games when Steam sends that field; it usually doesn't, so a
        # reviewer whose only review is this one stands in (decided 2026-11-10, chunk 2 review).
        if "num_games_owned" in author:
            owned_rows += 1
            if int(author["num_games_owned"]) <= config.FEW_GAMES_MAX:
                few += 1
        elif "num_reviews" in author:
            owned_rows += 1
            if int(author["num_reviews"]) <= 1:
                few += 1
        if r.get("received_for_free"):
            free += 1
        if r.get("language"):
            langs.add(str(r["language"]))
    total = len(rows)
    return {"n": total, "share_short_play": short / total, "share_few_games": (few / owned_rows) if owned_rows else None,
            "share_free": free / total, "languages": len(langs)}


def review_texts(client: Client, appid: int, n: int = config.VOICE_REVIEWS,
                 language: str = config.VOICE_LANGUAGE) -> list[dict[str, Any]]:
    """The n newest reviews in one language as [{text, up, hours}]; nothing else is read from the response.

    WHY: the player-voice summary needs what people wrote, not who they are. Same privacy rule as
    review_sample: author ids and every other field are never copied into the result, and the response is
    dropped. Pages with filter=1 (newest first) and follows the cursor until n rows, no rows, or a repeated cursor.
    """
    out: list[dict[str, Any]] = []
    cursor = "*"
    seen: set[str] = set()
    while len(out) < n and cursor not in seen:
        seen.add(cursor)
        params: dict[str, Any] = {"appid": appid, "filter": 1, "day_range": 0, "languages[0]": language,
                                  "purchase_type": 1, "num_per_page": 100, "cursor": cursor}
        resp = client.get_json(f"{API}/IUserReviewsService/GetAppReviews/v1/", params=params).get("response", {})
        rows = resp.get("reviews") or []
        if not rows:
            break
        for r in rows:
            if len(out) >= n:
                break
            out.append({"text": str(r.get("review") or "")[:config.VOICE_REVIEW_CHARS],
                        "up": bool(r.get("voted_up")),
                        "hours": round(int((r.get("author") or {}).get("playtime_forever", 0)) / 60, 1)})
        cursor = str(resp.get("cursor") or "")
        if not cursor:
            break
    return out
