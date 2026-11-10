"""Weekly digest: Scout (cheap model) drafts a concept per signal cluster, Skeptic (strong model) tries to kill it.

WHY: the radar finds clusters; a person wants three vetted ideas at most, each with checkable numbers.
Every claim passes radar.claims ("no link, no card"), every judged concept goes to the ledger so it is
never judged twice, and every API call goes through radar.llm's budget guard. Zero survivors is a valid
week: the digest never pads. A failed LLM stage still writes a digest, with a banner saying what happened.

CLI: python -m radar.digest --db radar.db --signals out/signals_<date>.json --brief BRIEF.md
        --ledger ledger.jsonl --out digests/ [--no-llm]
     python -m radar.digest appids --signals FILE      (comma-separated member appids, for twitch_snapshot)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import claims as claims_mod
from . import config, db
from .ledger import Ledger
from .llm import BudgetExceeded, Llm, LlmError, LlmRefusal, OutOfCredit, month_spend

log = logging.getLogger("radar.digest")

CLAIM_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["text", "value", "url"],
    "properties": {"text": {"type": "string"}, "value": {"type": "number"}, "url": {"type": "string"}}}
CLAIMS_SCHEMA: dict[str, Any] = {"type": "array", "items": CLAIM_SCHEMA}
STR_ARRAY: dict[str, Any] = {"type": "array", "items": {"type": "string"}}

SCOUT_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["title", "pitch", "core_mechanic", "twist", "inspired_by_appids", "claims", "solo_scope",
                 "prototype_plan"],
    "properties": {
        "title": {"type": "string"}, "pitch": {"type": "string"}, "core_mechanic": {"type": "string"},
        "twist": {"type": "string"},
        "inspired_by_appids": {"type": "array", "items": {"type": "integer"}},
        "claims": CLAIMS_SCHEMA,
        "solo_scope": {"type": "object", "additionalProperties": False, "required": ["art", "content", "systems"],
                       "properties": {"art": {"type": "string"}, "content": {"type": "string"},
                                      "systems": {"type": "string"}}},
        "prototype_plan": STR_ARRAY}}

CHECKS = ["clone_flood", "bot_growth", "solo_scope", "brief_fit", "weak_signal", "other"]
SKEPTIC_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["verdict", "kill_reasons", "claims", "scope_hours_estimate", "brief_fit", "brief_fit_notes",
                 "keep_case", "risks", "confidence"],
    "properties": {
        "verdict": {"type": "string", "enum": ["keep", "kill"]},
        "kill_reasons": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["check", "text"],
            "properties": {"check": {"type": "string", "enum": CHECKS}, "text": {"type": "string"}}}},
        "claims": CLAIMS_SCHEMA,
        "scope_hours_estimate": {"type": "integer"}, "brief_fit": {"type": "integer"},
        "brief_fit_notes": {"type": "string"}, "keep_case": {"type": "string"},
        "risks": STR_ARRAY, "confidence": {"type": "integer"}}}

SCOUT_SYSTEM = """You are a game concept scout for a solo Godot 4 developer.
You get one JSON document describing a cluster of small Steam games that are growing fast, plus the titles
of concepts already judged in earlier weeks (do not repeat them). Propose ONE original game concept that
borrows what works in the cluster and adds a real twist.
Rules:
- Every claim states exactly one number that appears in the JSON. Its url must be the store_url of the
  member that holds the number (or a listed clone game's store page, or the member's twitch url).
- Never invent numbers, games or links. Do not round or convert numbers except to a percent.
- Give 3 to 6 claims, each a fact from the document that supports why this concept is wanted.
- Be realistic about scope for one person: say what art, content and systems it needs.
- prototype_plan has one short string per step; it must fit a prototype of a couple of weeks."""

SKEPTIC_SYSTEM = """You are the Skeptic. Your job is to kill this game concept card. Keep it only if it survives every check.
You get the card, the signal cluster JSON it came from, and the developer BRIEF.
Checks:
(a) clone flood: read clone_pressure (count and top) and shared_tags. clone_pressure counts only near
    look-alikes (similarity >= 0.40 to a member); weigh how close the top ones are, not the count alone. Many
    near look-alikes unreleased means the niche is filling up.
(b) bot or paid growth: read each candidate's review_sample shares and red_flags. A thin account is a reviewer
    whose only review is this one (Steam does not return owned-games counts). Flagged shares are a reason to doubt.
(c) solo scope: work out the hour budget from the BRIEF's Time section (hours available times the maximum
    prototype weeks; read loose answers sensibly, e.g. "1 a day" is 7 hours a week). Kill if the prototype plan
    cannot produce a playable test in that budget using bought asset packs, or if art or content is something a
    solo dev cannot make. Multiplayer or networking counts heavily against when the BRIEF shows little
    networking experience.
(d) brief fit: genres, skills, platforms and scope limits in the BRIEF.
(e) weak signal: tiny review counts or a short history.
Claims you add as evidence follow the same rules as the card's: one number each, taken from the JSON, with the
store_url (or listed clone or twitch url) that holds it. Never invent numbers or links.
Fill scope_hours_estimate (integer hours for a playable test), brief_fit (1 to 5), confidence (1 to 5).
If the verdict is kill, give at least one kill_reason with the failing check."""

BILLING_URL = "https://console.anthropic.com/settings/billing"


@dataclass
class Vetting:
    """Everything the LLM stages produced; the digest is rendered from this."""
    clusters: list[dict[str, Any]] = field(default_factory=list)  # sent to the Scout
    valid: list[tuple[dict[str, Any], dict[str, Any]]] = field(default_factory=list)  # (cluster, card)
    judged: list[dict[str, Any]] = field(default_factory=list)  # cluster, card, skeptic
    notes: list[str] = field(default_factory=list)
    fatal: str | None = None


# ---------------------------------------------------------------- inputs

def load_signals(path: str) -> tuple[str, list[dict[str, Any]]]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data["week"], data["signals"]


def member_appids(signals: list[dict[str, Any]]) -> list[int]:
    return sorted({m["appid"] for s in signals for m in s["members"]})


def twitch_url(name: str) -> str:
    return f"https://www.twitch.tv/directory/game/{urllib.parse.quote(name)}"


def enrich_twitch(con: sqlite3.Connection, signals: list[dict[str, Any]], now: datetime | None = None) -> None:
    """Attach the newest twitch_live row younger than 24 h to each member that has one."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=config.TWITCH_CACHE_HOURS)).isoformat(timespec="seconds")
    for s in signals:
        for m in s["members"]:
            r = con.execute("""SELECT viewers, channels, fetched_at FROM twitch_live
                               WHERE appid=? AND fetched_at >= ? ORDER BY fetched_at DESC LIMIT 1""",
                            (m["appid"], cutoff)).fetchone()
            if r:
                m["twitch"] = {"viewers": r["viewers"], "channels": r["channels"], "fetched_at": r["fetched_at"],
                               "url": twitch_url(m["name"])}


def brief_budget(brief: str) -> str:
    """The two BRIEF lines that make the prototype budget, quoted as written."""
    hours = re.search(r"Hours per week available:\s*(.*)", brief)
    weeks = re.search(r"Max weeks for a prototype[^:]*:\s*(.*)", brief)
    return f"time: {hours.group(1).strip() if hours else '?'}; max weeks: {weeks.group(1).strip() if weeks else '?'}"


# ---------------------------------------------------------------- LLM stages

def scout(llm: Llm, cluster: dict[str, Any], titles: list[str]) -> tuple[dict[str, Any] | None, list[str]]:
    """One card for a cluster; one retry if its claims fail. Returns (card or None, last errors)."""
    user = json.dumps({"cluster": cluster, "already_judged_titles": titles}, ensure_ascii=False)
    card = llm.call_json(config.SCOUT_MODEL, config.SCOUT_EFFORT, config.SCOUT_MAX_TOKENS, SCOUT_SYSTEM, user,
                         SCOUT_SCHEMA, "scout")
    errors = claims_mod.check(card.get("claims"), cluster)
    if not errors:
        return card, []
    retry = (user + "\n\nYour previous answer:\n" + json.dumps(card, ensure_ascii=False)
             + "\n\nIt was rejected for these claim errors:\n- " + "\n- ".join(errors)
             + "\n\nAnswer again with corrected claims only citing numbers and urls from the JSON above.")
    card = llm.call_json(config.SCOUT_MODEL, config.SCOUT_EFFORT, config.SCOUT_MAX_TOKENS, SCOUT_SYSTEM, retry,
                         SCOUT_SCHEMA, "scout-retry")
    errors = claims_mod.check(card.get("claims"), cluster)
    return (card, []) if not errors else (None, errors)


def skeptic(llm: Llm, card: dict[str, Any], cluster: dict[str, Any], brief: str) -> dict[str, Any]:
    user = json.dumps({"card": card, "cluster": cluster}, ensure_ascii=False) + "\n\nBRIEF.md:\n" + brief
    return llm.call_json(config.SKEPTIC_MODEL, config.SKEPTIC_EFFORT, config.SKEPTIC_MAX_TOKENS, SKEPTIC_SYSTEM,
                         user, SKEPTIC_SCHEMA, "skeptic")


def fatal_text(exc: Exception) -> str:
    if isinstance(exc, OutOfCredit):
        return f"Anthropic API: out of credit, add credit at {BILLING_URL} ({exc})"
    if isinstance(exc, BudgetExceeded):
        return f"Spend cap reached, the next call was not made: {exc}"
    return f"Anthropic API error: {exc}"


def vet(llm: Llm, ledger: Ledger, clusters: list[dict[str, Any]], brief: str, week: str) -> Vetting:
    """Scout every cluster, then Skeptic the best valid cards; ledger lines are added as verdicts fall."""
    v = Vetting(clusters=clusters)
    titles = ledger.recent_titles()
    try:
        for c in clusters:
            try:
                card, errors = scout(llm, c, titles)
            except LlmRefusal:
                ledger.add(week, c, None, "refused", "the Scout refused")
                v.notes.append(f"Scout refused cluster {c['fingerprint']}; skipped.")
                continue
            if card is None:
                ledger.add(week, c, None, "invalid", "; ".join(errors))
                v.notes.append(f"Scout card for cluster {c['fingerprint']} had invalid claims after a retry; dropped.")
                continue
            v.valid.append((c, card))
        v.valid.sort(key=lambda cc: -cc[0]["score"])
        for c, card in v.valid[:config.MAX_SKEPTIC_CARDS]:
            try:
                sk = skeptic(llm, card, c, brief)
            except LlmRefusal:
                ledger.add(week, c, card["title"], "refused", "the Skeptic refused")
                v.notes.append(f"Skeptic refused {card['title']!r}; skipped.")
                continue
            good = []
            for cl in sk.get("claims") or []:
                errs = claims_mod.check([cl], c)
                if errs:
                    v.notes.append(f"Removed a Skeptic claim on {card['title']!r}: {errs[0]}")
                else:
                    good.append(cl)
            sk["claims"] = good
            v.judged.append({"cluster": c, "card": card, "skeptic": sk})
            if sk["verdict"] == "keep":
                ledger.add(week, c, card["title"], "keep", sk.get("keep_case", "")[:200])
            else:
                why = sk["kill_reasons"][0] if sk.get("kill_reasons") else {"check": "other", "text": ""}
                ledger.add(week, c, card["title"], "kill", f"{why['check']}: {why['text']}")
    except LlmError as exc:  # OutOfCredit, BudgetExceeded and plain errors all stop the LLM stages
        v.fatal = fatal_text(exc)
    return v


# ---------------------------------------------------------------- rendering

def money(cents: int | None, free: Any) -> str:
    if free or cents == 0:
        return "free"
    return "?" if cents is None else f"${cents / 100:.2f}"


def esc(s: Any) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def claim_lines(cl: list[dict[str, Any]]) -> list[str]:
    return [f"- {c['text']} — **{c['value']:g}** ([source]({c['url']}))" for c in cl]


def member_table(cluster: dict[str, Any]) -> list[str]:
    rows = ["| Game | Role | Released | Reviews | Positive | w0 → w1 | Price | Dev / pub apps |",
            "|---|---|---|---|---|---|---|---|"]
    for m in cluster["members"]:
        pos = "?" if m.get("positive_share") is None else f"{m['positive_share'] * 100:.0f}%"
        w = f"{m['w0']} → {m['w1']}" if m["role"] == "candidate" else "-"
        rows.append(f"| [{esc(m['name'])}]({m['store_url']}) | {m['role']} | {m['release_date']} | "
                    f"{m['reviews_total']} | {pos} | {w} | {money(m['price_cents'], m['is_free'])} | "
                    f"{m['dev_app_count']} / {m['pub_app_count']} |")
    return rows


def unvetted_lines(clusters: list[dict[str, Any]], heading: str) -> list[str]:
    out = [f"## {heading}", ""]
    for c in clusters:
        out += [f"### Cluster {c['fingerprint']} (score {c['score']})", "",
                f"Shared tags: {', '.join(c['shared_tags']) or '-'}; clone pressure: {c['clone_pressure']['count']}", ""]
        out += member_table(c) + [""]
    return out


def _why(k: dict[str, Any]) -> str:
    r = (k["skeptic"].get("kill_reasons") or [{"check": "other", "text": "no reason given"}])[0]
    return f"{r['check']}: {r['text']}"


def render(week: str, summary: str, banner: str | None, kept: list[dict[str, Any]], killed: list[dict[str, Any]],
           unvetted: list[dict[str, Any]], unvetted_heading: str, notes: list[str], brief: str,
           llm_ran: bool) -> str:
    out = [f"# Radar digest {week}", ""]
    if banner:
        out += [f"> **{banner}**", ""]
    out += [summary, ""]
    for i, k in enumerate(kept, 1):
        card, sk, c = k["card"], k["skeptic"], k["cluster"]
        out += [f"## {i}. {card['title']}", "", card["pitch"], "", f"**Twist:** {card['twist']}", "",
                f"**Core mechanic:** {card['core_mechanic']}", "", "**Inspired by**", ""]
        out += member_table(c) + [""]
        out += claim_lines(card["claims"] + sk["claims"]) + [""]
        out += [f"**Skeptic:** {sk['keep_case']}", ""]
        out += ["Risks:"] + [f"- {r}" for r in sk["risks"]] + [""]
        out += [f"Scope estimate: {sk['scope_hours_estimate']} h against the BRIEF budget ({brief_budget(brief)}). "
                f"Brief fit: {sk['brief_fit']}/5. Confidence: {sk['confidence']}/5. {sk['brief_fit_notes']}", ""]
        out += ["Prototype plan:"] + [f"{n}. {s}" for n, s in enumerate(card["prototype_plan"], 1)] + [""]
    if llm_ran and not kept and not banner:
        out += ["## No concept survived this week", "",
                f"The Skeptic killed all {len(killed)} card(s); the reasons are under Killed this week." if killed
                else "No card reached the Skeptic.", ""]
    if killed:
        out += ["## Killed this week", ""]
        for k in killed:
            cand = next((m for m in k["cluster"]["members"] if m["role"] == "candidate"), k["cluster"]["members"][0])
            out.append(f"- {k['card']['title']} ({_why(k)}) — [{esc(cand['name'])}]({cand['store_url']})")
        out.append("")
    if unvetted:
        out += unvetted_lines(unvetted, unvetted_heading)
    out += ["## Notes", ""] + [f"- {n}" for n in notes] + [""]
    return "\n".join(out)


def rank_kept(judged: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept = [j for j in judged if j["skeptic"]["verdict"] == "keep"]
    kept.sort(key=lambda j: (-j["skeptic"]["confidence"], -j["cluster"]["score"]))
    return kept


# ---------------------------------------------------------------- run

def run(db_path: str, signals_path: str, brief_path: str, ledger_path: str, out_dir: str,
        no_llm: bool = False, llm: Llm | None = None) -> int:
    """Write digests/<week>.md; exit code 1 when an LLM stage failed."""
    week, signals = load_signals(signals_path)
    with open(brief_path, encoding="utf-8") as f:
        brief = f.read()
    con = db.connect(db_path)
    enrich_twitch(con, signals)
    ledger = Ledger(ledger_path, date.fromisoformat(week))
    days = con.execute("SELECT COUNT(DISTINCT day) FROM daily WHERE ccu IS NOT NULL").fetchone()[0]
    twitch_n = sum(1 for s in signals for m in s["members"] if "twitch" in m)
    notes = [f"Player-count history: {days} distinct day(s) with data; a trend needs several weeks.",
             f"Twitch data present for {twitch_n} game(s)" + ("." if twitch_n else " (none: not configured or no match).")]
    banner = None
    code = 0
    kept: list[dict[str, Any]] = []
    killed: list[dict[str, Any]] = []
    unvetted: list[dict[str, Any]] = signals
    heading = "Unvetted signals (no LLM run)"
    scouted = valid = 0
    if not no_llm:
        fresh = [s for s in signals if not ledger.seen(s)]
        notes.append(f"{len(signals) - len(fresh)} cluster(s) skipped as already seen in the ledger.")
        todo = sorted(fresh, key=lambda s: (-s["score"], s["fingerprint"]))[:config.MAX_SCOUT_CLUSTERS]
        llm = llm or Llm(con=con)
        v = vet(llm, ledger, todo, brief, week)
        ledger.save()
        scouted, valid = len(v.clusters), len(v.valid)
        ranked = rank_kept(v.judged)
        kept = ranked[:config.MAX_DIGEST_CONCEPTS]
        if len(ranked) > len(kept):
            notes.append(f"{len(ranked) - len(kept)} more concept(s) survived; only {config.MAX_DIGEST_CONCEPTS} are shown.")
        killed = [j for j in v.judged if j["skeptic"]["verdict"] == "kill"]
        notes += v.notes
        unvetted = []
        if v.fatal:
            code = 1
            banner = v.fatal + f"; {len(v.judged)} of {len(v.clusters)} cluster(s) vetted"
            if not v.judged:
                banner += "; nothing was vetted"
            judged_fps = {j["cluster"]["fingerprint"] for j in v.judged}
            unvetted = [s for s in todo if s["fingerprint"] not in judged_fps]
            heading = "Unvetted signals (LLM stage stopped)"
            notes.append(v.fatal)
    spend_run = llm.run_spend if llm else 0.0
    if no_llm:
        summary = (f"{len(signals)} signal(s) found. No LLM run: nothing scouted or vetted. "
                   f"API spend this month ${month_spend(con):.2f}.")
    else:
        summary = (f"{len(signals)} signal(s) found, {scouted} scouted, {valid} valid card(s), {len(kept)} kept, "
                   f"{len(killed)} killed. API spend this run ${spend_run:.2f}, this month ${month_spend(con):.2f}.")
    text = render(week, summary, banner, kept, killed, unvetted, heading, notes, brief, not no_llm)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, f"{week}.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    con.close()
    print(f"digest: week={week} kept={len(kept)} killed={len(killed)} exit={code}")
    return code


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if argv and argv[0] == "appids":
        ap = argparse.ArgumentParser(prog="radar.digest appids")
        ap.add_argument("--signals", required=True)
        args = ap.parse_args(argv[1:])
        print(",".join(str(a) for a in member_appids(load_signals(args.signals)[1])))
        return 0
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="radar.db")
    ap.add_argument("--signals", required=True)
    ap.add_argument("--brief", default="BRIEF.md")
    ap.add_argument("--ledger", default="ledger.jsonl")
    ap.add_argument("--out", default="digests/")
    ap.add_argument("--no-llm", action="store_true")
    a = ap.parse_args(argv)
    return run(a.db, a.signals, a.brief, a.ledger, a.out, a.no_llm)


if __name__ == "__main__":
    sys.exit(main())
