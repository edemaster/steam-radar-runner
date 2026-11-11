"""Monthly market map: `python -m radar.market_map --db radar.db --brief BRIEF.md --out DIR [--no-llm] [--today D]`.

WHY: the weekly digest looks at single clusters; once a month this looks at the whole store through
tag pairs: which combinations are getting more releases (rising), which of those are already crowded
with hits (saturating), and where tiny teams had hits lately. The tables are pure arithmetic over the
database; the one LLM call only writes them up, and every number it cites must be a number from a row
(claims are verified like the digest's, dropping any bullet with a bad claim).
"""
from __future__ import annotations

import argparse
import itertools
import os
import statistics
import sys
from datetime import date, timedelta
from typing import Any

from . import db, detect
from .claims import _close
from .llm import Llm, LlmError

MODEL = "claude-opus-5-5"
EFFORT = "high"
MAX_TOKENS = 6000
RUN_CAP_USD = 3.0

TOP_TAGS = 6
RECENT_DAYS = 30
PRIOR_FROM_DAYS, PRIOR_TO_DAYS = 120, 31  # prior window: 31..120 days before today
MIN_PRIOR_RELEASES = 15
MIN_PRIOR_TINY = 10
HIT_REVIEWS = 500
MIN_GROWTH = 1.2
TABLE_ROWS = 20
RENDER_ROWS = 10
NUMBER_KEYS = ("recent_releases", "prior_releases_per_30d", "growth", "prior_median_reviews",
               "prior_hit_share", "tiny_hit_share", "prior_tiny_games")
SHARE_KEYS = ("prior_hit_share", "tiny_hit_share")

SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["headline", "sections"],
    "properties": {
        "headline": {"type": "string"},
        "sections": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["title", "bullets"],
            "properties": {"title": {"type": "string"}, "bullets": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["text", "claims"],
                "properties": {"text": {"type": "string"}, "claims": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["pair", "value"],
                    "properties": {"pair": {"type": "string"}, "value": {"type": "number"}}}}}}}}}}}}

SYSTEM = ("You write a one-page monthly market read for a solo game developer (the BRIEF below describes "
          "them). Say what rises, what saturates, where small teams win, and what that means for picking "
          "the next prototype. Use only the tables given. Every number you state must be a claim: "
          "{pair, value} where pair is a row key of a table (exactly as written) and value is one of that "
          "row's numbers. Shares may be quoted as fractions or percents. A bullet without a claim is dropped.")


# ---------------------------------------------------------------- tables

def compute(con: Any, today: date) -> dict[str, Any]:
    """The three ranked tables plus the pair count and window notes."""
    names = {r[0]: r[1] for r in con.execute("SELECT tagid, name FROM tags")}
    recent_from = today - timedelta(days=RECENT_DAYS)
    prior_from, prior_to = today - timedelta(days=PRIOR_FROM_DAYS), today - timedelta(days=PRIOR_TO_DAYS)
    rec: dict[str, int] = {}
    prior: dict[str, list[tuple[int | None, bool]]] = {}  # pair -> (store_reviews, is_tiny)
    rows = con.execute("SELECT * FROM apps WHERE type='game' AND coming_soon=0 AND release_date IS NOT NULL "
                       "AND release_date >= ? AND release_date <= ?",
                       (prior_from.isoformat(), today.isoformat())).fetchall()
    for a in rows:
        rd = date.fromisoformat(a["release_date"])
        if rd >= recent_from:
            where = rec
        elif prior_from <= rd <= prior_to:
            where = None
        else:
            continue
        tags = con.execute("SELECT tagid FROM app_tags WHERE appid=? ORDER BY weight DESC, tagid LIMIT ?",
                           (a["appid"], TOP_TAGS)).fetchall()
        keys = sorted({" + ".join(sorted((names.get(x[0], str(x[0])), names.get(y[0], str(y[0])))))
                       for x, y in itertools.combinations(tags, 2)})
        if not keys:
            continue
        tiny = not detect.tiny_failures(a)
        for k in keys:
            if where is rec:
                rec[k] = rec.get(k, 0) + 1
            else:
                prior.setdefault(k, []).append((a["store_reviews"], tiny))
    pairs: list[dict[str, Any]] = []
    for k, games in sorted(prior.items()):
        if len(games) < MIN_PRIOR_RELEASES:
            continue
        per30 = len(games) / 3
        have = [r for r, _ in games if r is not None]
        tiny = [r for r, t in games if t]
        pairs.append({
            "pair": k, "recent_releases": rec.get(k, 0), "prior_releases_per_30d": round(per30, 2),
            "growth": round(rec.get(k, 0) / per30, 2),
            "prior_median_reviews": statistics.median(have) if have else None,
            "prior_hit_share": round(sum(1 for r, _ in games if (r or 0) >= HIT_REVIEWS) / len(games), 3),
            "tiny_hit_share": round(sum(1 for r in tiny if (r or 0) >= HIT_REVIEWS) / len(tiny), 3)
            if tiny else None,
            "prior_tiny_games": len(tiny)})
    med_hit = statistics.median([p["prior_hit_share"] for p in pairs]) if pairs else 0.0
    grow = [p for p in pairs if p["growth"] >= MIN_GROWTH]
    rising = sorted(grow, key=lambda p: (-(p["growth"] * p["prior_hit_share"]), p["pair"]))
    saturating = sorted([p for p in grow if p["prior_hit_share"] < med_hit],
                        key=lambda p: (-p["growth"], p["pair"]))
    small = sorted([p for p in pairs if p["prior_tiny_games"] >= MIN_PRIOR_TINY],
                   key=lambda p: (-p["tiny_hit_share"], p["pair"]))
    return {"rising": rising[:TABLE_ROWS], "saturating": saturating[:TABLE_ROWS],
            "small_team_odds": small[:TABLE_ROWS], "pairs": len(pairs), "median_hit_share": med_hit,
            "windows": {"recent": [recent_from.isoformat(), today.isoformat()],
                        "prior": [prior_from.isoformat(), prior_to.isoformat()]}}


# ---------------------------------------------------------------- LLM write-up

def _rows(tables: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["pair"]: r for t in ("rising", "saturating", "small_team_odds") for r in tables[t]}


def claim_ok(claim: Any, rows: dict[str, dict[str, Any]]) -> bool:
    if not isinstance(claim, dict) or not isinstance(claim.get("pair"), str):
        return False
    v = claim.get("value")
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return False
    row = rows.get(claim["pair"])
    if row is None:
        return False
    for k in NUMBER_KEYS:
        n = row.get(k)
        if n is None:
            continue
        if _close(float(v), float(n)) or (k in SHARE_KEYS and _close(float(v), float(n) * 100)):
            return True
    return False


def write_up(llm: Llm, tables: dict[str, Any], brief: str) -> list[dict[str, Any]]:
    """Ask the model for the read; keep only bullets whose every claim is a number from a table row."""
    import json
    user = ("BRIEF:\n" + brief + "\n\nTABLES (json; windows and counts included):\n"
            + json.dumps(tables, ensure_ascii=False))
    data = llm.call_json(MODEL, EFFORT, MAX_TOKENS, SYSTEM, user, SCHEMA, "market_map")
    rows = _rows(tables)
    kept: list[dict[str, Any]] = [{"headline": str(data.get("headline", ""))}]
    for s in data.get("sections", []):
        bullets = [b for b in s.get("bullets", [])
                   if b.get("claims") and all(claim_ok(c, rows) for c in b["claims"])]
        if bullets:
            kept.append({"title": s.get("title", ""), "bullets": bullets})
    return kept


# ---------------------------------------------------------------- output

def _table(title: str, rows: list[dict[str, Any]]) -> list[str]:
    out = [f"### {title}", ""]
    if not rows:
        return out + ["_No pair qualifies._", ""]
    out += ["| Pair | Recent | Prior/30d | Growth | Median reviews | Hit share | Tiny games | Tiny hit share |",
            "|---|---|---|---|---|---|---|---|"]

    def f(v: Any, pct: bool = False) -> str:
        return "-" if v is None else (f"{v * 100:.0f}%" if pct else f"{v:g}")
    for r in rows[:RENDER_ROWS]:
        out.append(f"| {r['pair']} | {r['recent_releases']} | {f(r['prior_releases_per_30d'])} | "
                   f"{f(r['growth'])} | {f(r['prior_median_reviews'])} | {f(r['prior_hit_share'], True)} | "
                   f"{r['prior_tiny_games']} | {f(r['tiny_hit_share'], True)} |")
    return out + [""]


def render(month: str, tables: dict[str, Any], sections: list[dict[str, Any]] | None, spend: float,
           banner: str | None = None) -> str:
    """Markdown for the month; `sections` None means tables only."""
    out = [f"# Market map {month}", ""]
    if banner:
        out += [f"> **{banner}**", ""]
    if sections:
        head = sections[0].get("headline")
        if head:
            out += [f"**{head}**", ""]
        for s in sections[1:]:
            out += [f"## {s['title']}", ""] + [f"- {b['text']}" for b in s["bullets"]] + [""]
    out += ["## Tables", ""]
    out += _table("Rising (growth x hit share)", tables["rising"])
    out += _table("Saturating (growing, few hits per game)", tables["saturating"])
    out += _table("Small-team odds (tiny teams' share of 500+ review games)", tables["small_team_odds"])
    w = tables["windows"]
    out += ["## Notes", "",
            f"- Recent window {w['recent'][0]} to {w['recent'][1]}; prior window {w['prior'][0]} to "
            f"{w['prior'][1]} (a third of its count is the monthly rate). Pairs are among each game's top "
            f"{TOP_TAGS} tags; {tables['pairs']} pairs have at least {MIN_PRIOR_RELEASES} prior releases.",
            f"- A hit is {HIT_REVIEWS}+ store reviews; recent games have had little time to collect them, so "
            "hit shares use the prior window only. Games the collector never saw are missing, and a pair's "
            "growth on small counts is noisy.",
            f"- LLM spend this run: ${spend:.2f}.", ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--brief", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--today", default=None)
    a = ap.parse_args(argv)
    today = date.fromisoformat(a.today) if a.today else date.today()
    month = today.strftime("%Y-%m")
    con = db.connect(a.db)
    tables = compute(con, today)
    sections: list[dict[str, Any]] | None = None
    banner = None
    spend = 0.0
    code = 0
    if not a.no_llm:
        llm = Llm(con=con, run_cap=RUN_CAP_USD)
        try:
            brief = open(a.brief, encoding="utf-8").read() if a.brief and os.path.exists(a.brief) else ""
            sections = write_up(llm, tables, brief)
        except LlmError as exc:
            banner = f"The write-up stopped ({type(exc).__name__}: {exc}); tables only."
            code = 1
        spend = llm.run_spend
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, f"{month}.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(render(month, tables, sections, spend, banner))
    bullets = sum(len(s["bullets"]) for s in (sections or [])[1:])
    print(f"market_map: month={month} pairs={tables['pairs']} bullets={bullets} spend=${spend:.2f}")
    return code


if __name__ == "__main__":
    sys.exit(main())
