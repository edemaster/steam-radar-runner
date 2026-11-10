"""Second-success detector: `python -m radar.detect --db radar.db [--week YYYY-MM-DD] [--report] [--out out/]`.

WHY: finds recent fast-growing tiny-team games with an unusual tag mix that resemble a tiny-team
hit of the last year. Pure arithmetic over stored counts, no LLM. Every threshold lives in config.py.
The signals JSON written here is the only input the later LLM stages see, so it carries every number
with its source table.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import logging
import math
import os
import re
import sqlite3
import statistics
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import config, db, series, steam
from .http import Client, HttpError

log = logging.getLogger("radar.detect")

STOPWORDS = frozenset("""the and for with you your that this are from will can has have not but all out one
into their them they its our more when what who how his her was were been being get got use used using any
each than then there these those which also only over such just very new well may must both etc too own
make made like way ways off per via yet about after before while where why would could should""".split())

Vec = dict[int, float]
Pairs = list[tuple[int, int, int, float]]  # (tag_a, tag_b, games with both, share of tagged games)


# ---------------------------------------------------------------- rules on one game

def tiny_failures(a: sqlite3.Row) -> list[str]:
    """Failed tiny-team sub-rules (an empty list means tiny)."""
    bad = []
    if a["dev_app_count"] is None or a["dev_app_count"] > config.TINY_MAX_DEV_APPS:
        bad.append("dev_count")
    if not (a["self_published"] == 1 or (a["pub_app_count"] is not None
                                         and a["pub_app_count"] <= config.TINY_MAX_PUB_APPS)):
        bad.append("publisher")
    if not (a["is_free"] == 1 or (a["price_cents"] is not None
                                  and a["price_cents"] <= config.TINY_MAX_PRICE_CENTS)):
        bad.append("price")
    return bad


class Game:
    """One evaluated game: its row, review series, role (by age) and the rules it failed.

    Roles: age <= CAND_MAX_DAYS is a candidate, older (up to OLD_HIT_MAX_DAYS) an old hit, so a game
    of exactly 90 days is a candidate and never both.
    """

    def __init__(self, row: sqlite3.Row, week: date, pts: list[series.Point]) -> None:
        self.row = row
        self.appid: int = row["appid"]
        self.release = date.fromisoformat(row["release_date"])
        self.age = (week - self.release).days
        self.series = pts
        cur = series.value_at(pts, week)
        self.total, self.positive = (cur[1], cur[2]) if cur else (None, None)
        self.share = (self.positive / self.total) if self.total else None
        self.w1: int | None = None
        self.w0: int | None = None
        self.growth = 0.0
        self.role = "old_hit" if self.age > config.CAND_MAX_DAYS else "candidate"
        self.devs: frozenset[str] = frozenset(json.loads(row["developers"] or "[]"))
        self.fails: list[str] = tiny_failures(row)
        self._evaluate(week)

    def _evaluate(self, week: date) -> None:
        f = self.fails
        if self.total is None:
            f.append("no_series")
            return
        if self.share is None or self.share < config.MIN_POSITIVE_SHARE:
            f.append("positive_share")
        if self.role == "old_hit":
            if self.total < config.OLD_HIT_MIN_REVIEWS:
                f.append("reviews")
            return
        if self.total < config.CAND_MIN_REVIEWS:
            f.append("reviews")
        if self.age < config.CAND_YOUNG_MIN_AGE:
            f.append("too_young")
        elif self.age < config.CAND_STEADY_MIN_AGE:
            first = series.reviews_between(self.series, self.release, self.release + timedelta(days=7))
            self.w1, self.w0 = first, 0
            if first is None or first < config.CAND_YOUNG_FIRST_WEEK:
                f.append("growth")
        else:
            self.w1 = series.window(self.series, week, 7)
            self.w0 = series.reviews_between(self.series, week - timedelta(days=14),
                                             week - timedelta(days=7))
            if self.w1 is None or self.w0 is None or self.w1 < config.CAND_W1_MIN or self.w1 < self.w0:
                f.append("growth")
        if self.w1 is not None and self.w0 is not None:
            self.growth = math.log2((self.w1 + 1) / (self.w0 + 1)) + math.log10(self.w1 + 1)


def ccu_trend(con: sqlite3.Connection, appid: int, week: date) -> tuple[int | None, float | None]:
    """(latest ccu, median of the last 3 days / median of the 3 days a week earlier).

    The trend is a bonus only (never a rule) and needs CCU_MIN_POINTS daily points.
    """
    rows = [(date.fromisoformat(r[0]), r[1]) for r in con.execute(
        "SELECT day, ccu FROM daily WHERE appid=? AND ccu IS NOT NULL AND day<=? ORDER BY day",
        (appid, week.isoformat()))]
    if not rows:
        return None, None
    latest = rows[-1][1]
    if len(rows) < config.CCU_MIN_POINTS:
        return latest, None

    def med(lo: int, hi: int) -> float | None:
        v = [c for d, c in rows if week - timedelta(days=lo) <= d <= week - timedelta(days=hi)]
        return statistics.median(v) if v else None
    now, before = med(2, 0), med(9, 7)
    return latest, (now / before if now is not None and before else None)


# ---------------------------------------------------------------- tags and text

def tokens(text: str) -> list[str]:
    """Lowercase words of 3+ letters minus stopwords."""
    return [w for w in re.findall(r"[a-z]{3,}", (text or "").lower()) if w not in STOPWORDS]


class Corpus:
    """Tag IDF, per-tag game sets and description IDF over all games (loaded once per run)."""

    def __init__(self, con: sqlite3.Connection) -> None:
        self.con = con
        self.n_tagged: int = con.execute("SELECT COUNT(DISTINCT t.appid) FROM app_tags t JOIN apps a "
                                         "ON a.appid=t.appid WHERE a.type='game'").fetchone()[0]
        self.df: dict[int, int] = {r[0]: r[1] for r in con.execute(
            "SELECT t.tagid, COUNT(*) FROM app_tags t JOIN apps a ON a.appid=t.appid "
            "WHERE a.type='game' GROUP BY t.tagid")}
        self.names: dict[int, str] = {r[0]: r[1] for r in con.execute("SELECT tagid, name FROM tags")}
        self._tag_apps: dict[int, frozenset[int]] = {}
        self._tags: dict[int, list[tuple[int, int]]] = {}
        self.desc_df: Counter[str] = Counter()
        self.n_desc = 0
        for (desc,) in con.execute("SELECT short_desc FROM apps WHERE type='game' AND short_desc != ''"):
            self.n_desc += 1
            self.desc_df.update(set(tokens(desc)))

    def tags_of(self, appid: int) -> list[tuple[int, int]]:
        """(tagid, weight) by weight desc, ties by tagid."""
        if appid not in self._tags:
            rows = self.con.execute("SELECT tagid, weight FROM app_tags WHERE appid=?", (appid,)).fetchall()
            self._tags[appid] = sorted(((r[0], r[1] or 0) for r in rows), key=lambda x: (-x[1], x[0]))
        return self._tags[appid]

    def idf(self, tagid: int) -> float:
        return math.log(self.n_tagged / self.df[tagid]) if self.df.get(tagid) else 0.0

    def tag_vec(self, appid: int) -> Vec:
        """Top tags by weight x idf, L2-normalised."""
        v = {t: w * self.idf(t) for t, w in self.tags_of(appid)[:config.TAG_TOP_VECTOR]}
        norm = math.sqrt(sum(x * x for x in v.values()))
        return {t: x / norm for t, x in v.items() if x > 0} if norm else {}

    def text_vec(self, desc: str) -> dict[str, float]:
        """Description TF-IDF, L2-normalised."""
        tf = Counter(tokens(desc))
        v = {w: c * math.log(self.n_desc / self.desc_df[w]) for w, c in tf.items() if self.desc_df.get(w)}
        norm = math.sqrt(sum(x * x for x in v.values()))
        return {w: x / norm for w, x in v.items()} if norm else {}

    def tag_apps(self, tagid: int) -> frozenset[int]:
        if tagid not in self._tag_apps:
            self._tag_apps[tagid] = frozenset(r[0] for r in self.con.execute(
                "SELECT t.appid FROM app_tags t JOIN apps a ON a.appid=t.appid "
                "WHERE t.tagid=? AND a.type='game'", (tagid,)))
        return self._tag_apps[tagid]

    def rare_pairs(self, appid: int) -> Pairs:
        """Pairs among the top UNUSUAL_TOP_TAGS tags with how many games carry both, rarest first."""
        top = [t for t, _ in self.tags_of(appid)[:config.UNUSUAL_TOP_TAGS]]
        out: Pairs = []
        for a, b in itertools.combinations(top, 2):
            n = len(self.tag_apps(a) & self.tag_apps(b))
            out.append((a, b, n, n / self.n_tagged if self.n_tagged else 1.0))
        return sorted(out, key=lambda p: (p[3], p[0], p[1]))


def weighted_jaccard(a: Vec, b: Vec) -> float:
    """sum(min) / sum(max) over the union of keys."""
    keys = sorted(a.keys() | b.keys())
    den = sum(max(a.get(k, 0.0), b.get(k, 0.0)) for k in keys)
    return sum(min(a.get(k, 0.0), b.get(k, 0.0)) for k in keys) / den if den else 0.0


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    """Dot product of two L2-normalised sparse vectors."""
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(k, 0.0) for k, x in sorted(a.items()))


class Sim:
    """Similarity between two games: 0.7 x weighted Jaccard of tag vectors + 0.3 x description cosine."""

    def __init__(self, corpus: Corpus) -> None:
        self.c = corpus
        self._v: dict[int, tuple[Vec, dict[str, float]]] = {}

    def _vecs(self, appid: int) -> tuple[Vec, dict[str, float]]:
        if appid not in self._v:
            row = self.c.con.execute("SELECT short_desc FROM apps WHERE appid=?", (appid,)).fetchone()
            self._v[appid] = (self.c.tag_vec(appid), self.c.text_vec(row[0] if row and row[0] else ""))
        return self._v[appid]

    def __call__(self, a: int, b: int) -> float:
        ta, da = self._vecs(a)
        tb, db_ = self._vecs(b)
        return config.SIM_TAG_WEIGHT * weighted_jaccard(ta, tb) + config.SIM_DESC_WEIGHT * cosine(da, db_)


# ---------------------------------------------------------------- clustering

def fingerprint(appids: list[int]) -> str:
    """Stable id of a member set: sha1 of the sorted appids joined by commas, 12 hex chars."""
    return hashlib.sha1(",".join(str(a) for a in sorted(appids)).encode()).hexdigest()[:12]


def components(nodes: list[int], edges: list[tuple[int, int]]) -> list[list[int]]:
    """Connected components, each sorted, ordered by their smallest appid."""
    parent = {n: n for n in nodes}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for a, b in edges:
        parent[find(a)] = find(b)
    groups: dict[int, list[int]] = {}
    for n in nodes:
        groups.setdefault(find(n), []).append(n)
    return sorted((sorted(g) for g in groups.values()), key=lambda g: g[0])


def _best(comp: list[int], sims: dict[tuple[int, int], float]) -> dict[int, tuple[float, int]]:
    """Each member's highest edge similarity inside comp and its partner."""
    return {a: max(((sims[(a, b)], b) for b in comp if (a, b) in sims), default=(0.0, 0)) for a in comp}


def build_signals(cands: list[Game], olds: list[Game], sim: Sim) -> list[dict[str, Any]]:
    """Cluster candidates and old hits; every component with both roles becomes a signal.

    Edges: candidate-old hit and candidate-candidate at similarity >= SIM_MIN, never between games that
    share a developer: a sequel or port of one's own hit is not a second team's success (the first live
    run's only signal was a sequel, 2026-11-11). A component over
    MAX_CLUSTER keeps the members most similar to their best match, but never loses its last candidate
    or last old hit (the weakest kept member makes room).
    """
    games = {g.appid: g for g in cands + olds}
    sims: dict[tuple[int, int], float] = {}
    edges = []
    for c in cands:
        for o in cands + olds:
            if o.appid == c.appid or (o.role == "candidate" and o.appid < c.appid) or (c.devs & o.devs):
                continue
            s = sim(c.appid, o.appid)
            if s >= config.SIM_MIN:
                sims[(c.appid, o.appid)] = sims[(o.appid, c.appid)] = s
                edges.append((c.appid, o.appid))
    out = []
    for comp in components(sorted(games), edges):
        roles = {games[a].role for a in comp}
        if roles != {"candidate", "old_hit"}:
            continue
        if len(comp) > config.MAX_CLUSTER:
            best = _best(comp, sims)
            ranked = sorted(comp, key=lambda a: (-best[a][0], a))
            keep = ranked[:config.MAX_CLUSTER]
            for r in ("candidate", "old_hit"):
                if not any(games[a].role == r for a in keep):
                    keep[-1] = next(a for a in ranked if games[a].role == r)
            comp = sorted(keep)
        best = _best(comp, sims)
        in_cands = [games[a] for a in comp if games[a].role == "candidate"]
        mean_sim = statistics.mean(best[a][0] for a in comp)
        out.append({"appids": comp, "best": best,
                    "score": max(g.growth for g in in_cands) + 2 * len(in_cands) + mean_sim})
    return out


def shared_tags(corpus: Corpus, appids: list[int]) -> list[int]:
    """Tags in every member's top 10, else the 5 with the highest summed weight."""
    tops = [{t for t, _ in corpus.tags_of(a)[:config.SHARED_TAG_TOP]} for a in appids]
    common = set.intersection(*tops) if tops else set()
    if common:
        return sorted(common)
    total: Counter[int] = Counter()
    for a in appids:
        for t, w in corpus.tags_of(a):
            total[t] += w
    return [t for t, _ in sorted(total.items(), key=lambda x: (-x[1], x[0]))[:config.SHARED_TAG_FALLBACK]]


def clone_pressure(con: sqlite3.Connection, sim: Sim, members: list[int]) -> dict[str, Any]:
    """Coming-soon games with similarity >= CLONE_SIM_MIN to any member: count and the top 10."""
    found = []
    rows = con.execute("""SELECT appid, name, release_date FROM apps
                          WHERE coming_soon=1 AND type='game'
                            AND appid IN (SELECT appid FROM app_tags) ORDER BY appid""").fetchall()
    for r in rows:
        best = max((sim(r["appid"], m) for m in members if m != r["appid"]), default=0.0)
        if best >= config.CLONE_SIM_MIN:
            found.append({"appid": r["appid"], "name": r["name"], "sim": round(best, 4),
                          "release_date": r["release_date"]})
    found.sort(key=lambda x: (-x["sim"], x["appid"]))
    return {"count": len(found), "top": found[:config.CLONE_TOP]}


# ---------------------------------------------------------------- run

def load_games(con: sqlite3.Connection, week: date) -> list[Game]:
    """Every released game in the candidate or old-hit age window, evaluated against all rules."""
    lo = (week - timedelta(days=config.OLD_HIT_MAX_DAYS)).isoformat()
    hi = (week - timedelta(days=1)).isoformat()
    rows = con.execute("""SELECT * FROM apps WHERE type='game' AND coming_soon=0
                          AND release_date BETWEEN ? AND ? ORDER BY appid""", (lo, hi)).fetchall()
    return [Game(r, week, series.cumulative(con, r["appid"])) for r in rows]


def store_sample(con: sqlite3.Connection, appid: int, day: date, s: dict[str, Any]) -> None:
    con.execute("INSERT OR REPLACE INTO review_samples VALUES(?,?,?,?,?,?,?)",
                (appid, day.isoformat(), s["n"], s["share_short_play"], s["share_few_games"],
                 s["share_free"], s["languages"]))


def sync_watch(con: sqlite3.Connection, old_hits: set[int]) -> None:
    """Old hits are tracked daily by collect; rows no longer qualifying are dropped."""
    con.executemany("INSERT OR IGNORE INTO watch VALUES(?, 'old_hit')", [(a,) for a in sorted(old_hits)])
    for (a,) in con.execute("SELECT appid FROM watch WHERE reason='old_hit'").fetchall():
        if a not in old_hits:
            con.execute("DELETE FROM watch WHERE appid=? AND reason='old_hit'", (a,))


def detect(con: sqlite3.Connection, week: date, client: Client | None = None,
           report: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Run every rule for `week`; write signals and the watch list; return signal documents, best first."""
    corpus = Corpus(con)
    games = load_games(con, week)
    olds = [g for g in games if g.role == "old_hit" and not g.fails]
    pre = [g for g in games if g.role == "candidate" and not g.fails]
    pairs: dict[int, Pairs] = {g.appid: corpus.rare_pairs(g.appid) for g in pre}
    cands = [g for g in pre if pairs[g.appid] and pairs[g.appid][0][3] <= config.UNUSUAL_PAIR_MAX_SHARE]
    sim = Sim(corpus)
    raw = build_signals(cands, olds, sim)
    raw.sort(key=lambda s: (-s["score"], s["appids"]))
    by_id = {g.appid: g for g in olds + cands}
    sync_watch(con, {g.appid for g in olds})

    docs = []
    con.execute("DELETE FROM signals WHERE week=?", (week.isoformat(),))
    for s in raw:
        samples: dict[int, dict[str, Any]] = {}
        for a in s["appids"]:
            if by_id[a].role == "candidate" and client is not None:
                try:
                    sm = steam.review_sample(client, a)
                except HttpError as exc:
                    log.error("review sample failed for %d: %s", a, exc)
                    sm = None
                if sm:
                    store_sample(con, a, week, sm)
                    samples[a] = sm
        doc = signal_doc(con, corpus, week, s, by_id, pairs, samples, sim)
        docs.append(doc)
        con.execute("INSERT INTO signals VALUES(?,?,?,?,?,?,?)",
                    (week.isoformat(), doc["fingerprint"], json.dumps(s["appids"]),
                     json.dumps({str(m["appid"]): m["role"] for m in doc["members"]}),
                     json.dumps(doc["shared_tags"]), doc["score"], json.dumps(doc)))
    con.commit()
    if report is not None:
        report.update(summarise(con, corpus, games, olds, pre, cands, docs, pairs))
    return docs


def red_flags(sample: dict[str, Any] | None) -> list[str]:
    """Review-sample shares at or past the config limits, as readable strings (computed here, not by the LLM)."""
    if not sample:
        return []
    out = []
    for key, limit in (("share_free", config.RED_FLAG_SHARE_FREE),
                       ("share_short_play", config.RED_FLAG_SHARE_SHORT_PLAY),
                       ("share_few_games", config.RED_FLAG_SHARE_FEW_GAMES)):
        v = sample.get(key)
        if v is not None and v >= limit:
            out.append(f"{key} {v:.2f} >= {limit:.2f}")
    n, langs = sample.get("n") or 0, sample.get("languages")
    if langs is not None and n >= config.RED_FLAG_LANGUAGES_MIN_N and langs <= config.RED_FLAG_MAX_LANGUAGES:
        out.append(f"languages {langs} <= {config.RED_FLAG_MAX_LANGUAGES} with n={n}")
    return out


def signal_doc(con: sqlite3.Connection, corpus: Corpus, week: date, s: dict[str, Any], by_id: dict[int, Game],
               pairs: dict[int, Pairs], samples: dict[int, dict[str, Any]], sim: Sim) -> dict[str, Any]:
    """The JSON document for one signal; `sources` names the table behind each number."""
    members = []
    for a in s["appids"]:
        g = by_id[a]
        r = g.row
        ccu, trend = ccu_trend(con, a, week)
        m: dict[str, Any] = {
            "appid": a, "role": g.role, "name": r["name"],
            "store_url": f"https://store.steampowered.com/app/{a}/",
            "release_date": r["release_date"], "price_cents": r["price_cents"], "is_free": r["is_free"],
            "developers": json.loads(r["developers"] or "[]"),
            "publishers": json.loads(r["publishers"] or "[]"),
            "dev_app_count": r["dev_app_count"], "pub_app_count": r["pub_app_count"],
            "reviews_total": g.total, "reviews_positive": g.positive,
            "sim_to_best_match": round(s["best"][a][0], 4), "best_match_appid": s["best"][a][1],
            "ccu_latest": ccu, "ccu_trend": None if trend is None else round(trend, 3),
            "short_desc": r["short_desc"],
            "tags": [x["name"] for x in con.execute(
                """SELECT t.name FROM app_tags a JOIN tags t ON t.tagid=a.tagid WHERE a.appid=?
                   ORDER BY a.weight DESC, a.tagid LIMIT 8""", (a,)).fetchall()],
            "positive_share": round(g.positive / g.total, 3) if g.total else None,
        }
        if g.role == "candidate":
            names = corpus.names
            m.update({"w1": g.w1, "w0": g.w0, "growth_score": round(g.growth, 4),
                      "unusual_pairs": [{"tags": [names.get(p[0], str(p[0])), names.get(p[1], str(p[1]))],
                                         "games_with_both": p[2], "share": round(p[3], 5)}
                                        for p in pairs[a][:3]],
                      "review_sample": samples.get(a), "red_flags": red_flags(samples.get(a))})
        members.append(m)
    return {
        "week": week.isoformat(), "fingerprint": fingerprint(s["appids"]), "score": round(s["score"], 4),
        "shared_tags": [corpus.names.get(t, str(t)) for t in shared_tags(corpus, s["appids"])],
        "members": members,
        "clone_pressure": clone_pressure(con, sim, s["appids"]),
        "sources": {
            "reviews_total/positive, w1, w0": "review_points + daily (radar/series.py), last point <= date",
            "ccu_latest, ccu_trend": "daily.ccu",
            "release_date, price, developers, publishers, *_app_count": "apps",
            "unusual_pairs, shared_tags": "app_tags + tags (games with both / games with tags)",
            "review_sample": "review_samples (aggregates of the 100 newest reviews)",
            "clone_pressure": "apps.coming_soon = 1, similarity >= CLONE_SIM_MIN (near look-alikes)",
            "thresholds": "radar/config.py"},
    }


def summarise(con: sqlite3.Connection, corpus: Corpus, games: list[Game], olds: list[Game], pre: list[Game],
              cands: list[Game], docs: list[dict[str, Any]], pairs: dict[int, Pairs]) -> dict[str, Any]:
    """Tuning numbers: the funnel counts and the nearest misses per rule."""
    tiny = con.execute("""SELECT COUNT(*) FROM apps WHERE type='game' AND coming_soon=0
                          AND dev_app_count <= ? AND (self_published=1 OR pub_app_count <= ?)
                          AND (is_free=1 OR price_cents <= ?)""",
                       (config.TINY_MAX_DEV_APPS, config.TINY_MAX_PUB_APPS, config.TINY_MAX_PRICE_CENTS)
                       ).fetchone()[0]
    kept = {g.appid for g in cands}
    pre_ids = {g.appid for g in pre}
    misses: dict[str, list[dict[str, Any]]] = {}
    for g in games:
        if g.appid in pre_ids and g.appid not in kept:
            misses.setdefault("candidate:unusual", []).append(_miss(g, pairs[g.appid][0][3] if pairs[g.appid] else None))
        elif len(g.fails) == 1:
            misses.setdefault(f"{g.role}:{g.fails[0]}", []).append(_miss(g, None))
    for k in misses:
        misses[k].sort(key=lambda m: (-(m["reviews"] or 0), m["appid"]))
        misses[k] = misses[k][:10]
    return {"games_with_tags": corpus.n_tagged, "tiny_team_games": tiny, "old_hits": len(olds),
            "candidates_before_unusual": len(pre), "candidates_after_unusual": len(cands),
            "signals": len(docs), "nearest_misses": misses}


def _miss(g: Game, share: float | None) -> dict[str, Any]:
    return {"appid": g.appid, "name": g.row["name"], "age": g.age, "reviews": g.total,
            "positive_share": None if g.share is None else round(g.share, 3), "w1": g.w1, "w0": g.w0,
            "rarest_pair_share": share}


def print_report(rep: dict[str, Any]) -> None:
    """Short tuning summary to stdout."""
    print("detect report")
    for k in ("games_with_tags", "tiny_team_games", "old_hits", "candidates_before_unusual",
              "candidates_after_unusual", "signals"):
        print(f"  {k}: {rep[k]}")
    for rule, ms in sorted(rep["nearest_misses"].items()):
        print(f"  nearest misses failing only [{rule}]:")
        for m in ms:
            print(f"    {m['appid']} {m['name']!r} age={m['age']}d reviews={m['reviews']} "
                  f"pos={m['positive_share']} w1={m['w1']} w0={m['w0']}")


def run(db_path: str, week: date | None = None, report: bool = False, out: str | None = None,
        client: Client | None = None) -> int:
    """CLI body."""
    week = week or datetime.now(timezone.utc).date()
    con = db.connect(db_path)
    rep: dict[str, Any] | None = {} if report else None
    docs = detect(con, week, client or Client(), rep)
    if rep is not None:
        print_report(rep)
    if out:
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, f"signals_{week.isoformat()}.json"), "w", encoding="utf-8") as f:
            json.dump({"week": week.isoformat(), "signals": docs}, f, indent=1, ensure_ascii=False)
    con.close()
    print(f"detect: week={week} signals={len(docs)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="radar.db")
    ap.add_argument("--week", default=None, help="as-of date YYYY-MM-DD (default: today UTC)")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return run(args.db, date.fromisoformat(args.week) if args.week else None, args.report, args.out)


if __name__ == "__main__":
    sys.exit(main())
