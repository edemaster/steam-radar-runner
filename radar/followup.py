"""Follow-up check: does a signal keep growing after we judged it? `python -m radar.followup --db --ledger --out --today`.

WHY: kills and keeps are only opinions until the games' own review counts confirm or refute them; this
turns the ledger into a scoreboard (no LLM, free). Each (fingerprint, horizon) is checked once; results
are appended to a JSONL file in the data repo.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import date, timedelta
from typing import Any

from . import db, series

HORIZONS = (28, 56)
HELD_W1_SHARE = 0.5  # a game held if its last-7-day reviews are at least this share of when we judged it


def _read(path: str) -> list[dict[str, Any]]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _game_outcome(then: dict[str, Any], now_total: int | None, w1_now: int | None) -> str:
    rt, w1t = then.get("reviews_total"), then.get("w1")
    if rt is None or w1t is None or now_total is None or w1_now is None:
        return "unknown"
    return "held" if w1_now >= HELD_W1_SHARE * w1t and now_total > rt else "faded"


def check(con: sqlite3.Connection, ledger_path: str, followup_path: str, today: date) -> list[dict[str, Any]]:
    """Append and return the follow-up rows that are due and not yet written."""
    done = {(r["fingerprint"], r["horizon_days"]) for r in _read(followup_path)}
    new: list[dict[str, Any]] = []
    for e in _read(ledger_path):
        snap = e.get("snapshot")
        if not snap:
            continue
        week = date.fromisoformat(e["week"])
        for h in HORIZONS:
            if week + timedelta(days=h) > today or (e["fingerprint"], h) in done:
                continue
            games: dict[str, Any] = {}
            for appid, then in snap.items():
                pts = series.cumulative(con, int(appid))
                cur = series.value_at(pts, today)
                now_total = cur[1] if cur else None
                w1_now = series.window(pts, today, 7)
                games[appid] = {"reviews_then": then.get("reviews_total"), "reviews_now": now_total,
                                "w1_then": then.get("w1"), "w1_now": w1_now,
                                "outcome": _game_outcome(then, now_total, w1_now)}
            outs = [g["outcome"] for g in games.values()]
            outcome = "held" if "held" in outs else ("faded" if outs and all(o == "faded" for o in outs)
                                                     else "unknown")
            new.append({"checked": today.isoformat(), "week": e["week"], "fingerprint": e["fingerprint"],
                        "title": e.get("title"), "verdict": e.get("verdict"),
                        "reason_check": (e.get("reason") or "").split(":", 1)[0].strip(),
                        "horizon_days": h, "outcome": outcome, "games": games})
    if new:
        os.makedirs(os.path.dirname(os.path.abspath(followup_path)), exist_ok=True)
        with open(followup_path, "a", encoding="utf-8", newline="\n") as f:
            for r in new:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return new


def _share(rows: list[dict[str, Any]]) -> str:
    held = sum(r["outcome"] == "held" for r in rows)
    return f"{held}/{len(rows)} held ({100 * held / len(rows):.0f}%)"


def summary_lines(new_rows: list[dict[str, Any]], followup_path: str) -> list[str]:
    """Markdown for the digest; empty when there is nothing new and nothing to tally."""
    long_rows = [r for r in _read(followup_path) if r.get("horizon_days") == max(HORIZONS)]
    if not new_rows and not long_rows:
        return []
    out = ["## Follow-up", ""]
    for r in new_rows:
        nums = ", ".join(f"{a}: reviews {g['reviews_then']} -> {g['reviews_now']}, "
                         f"w1 {g['w1_then']} -> {g['w1_now']}" for a, g in r["games"].items())
        out.append(f"- **{r.get('title') or r['fingerprint']}** ({r['verdict']}, judged {r['week']}): "
                   f"after {r['horizon_days']} days **{r['outcome']}** ({nums})")
    if long_rows:
        out += ["", f"Running tally at {max(HORIZONS)} days:"]
        for v in ("keep", "kill"):
            rows = [r for r in long_rows if r["verdict"] == v]
            if rows:
                out.append(f"- {v}: {_share(rows)}")
        checks = sorted({r["reason_check"] for r in long_rows if r["verdict"] == "kill" and r["reason_check"]})
        for c in checks:
            out.append(f"- kill by `{c}`: " + _share([r for r in long_rows
                                                     if r["verdict"] == "kill" and r["reason_check"] == c]))
    return out + [""]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--today", default=None)
    a = ap.parse_args(argv)
    today = date.fromisoformat(a.today) if a.today else date.today()
    con = db.connect(a.db)
    print(f"followup: new={len(check(con, a.ledger, a.out, today))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
