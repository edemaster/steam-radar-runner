import os
import tempfile
import unittest
from datetime import date, timedelta

from radar import db, market_map
from radar.llm import Llm

from .fakes import FakeAnthropic, Msg

TODAY = date(2026, 12, 1)
M = market_map.MODEL
_next = [1000]


def add(con, days_ago, tags, reviews=None, tiny=True, coming_soon=0, typ="game"):
    _next[0] += 1
    a = _next[0]
    con.execute("INSERT INTO apps(appid, name, type, release_date, coming_soon, developers, publishers, "
                "self_published, dev_app_count, pub_app_count, price_cents, is_free, store_reviews) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (a, f"g{a}", typ, (TODAY - timedelta(days=days_ago)).isoformat(), coming_soon, "[]", "[]",
                 1 if tiny else 0, 1 if tiny else 50, 1 if tiny else 50, 999, 0, reviews))
    con.executemany("INSERT INTO app_tags VALUES(?,?,?)", [(a, t, 100 - i) for i, t in enumerate(tags)])


def world():
    con = db.connect(os.path.join(tempfile.mkdtemp(), "t.db"))
    con.executemany("INSERT INTO tags VALUES(?,?)", [(1, "Roguelike"), (2, "Cozy"), (3, "Farming"), (4, "Zeta")])
    # Cozy + Roguelike: 30 prior (6 hits, 20 tiny incl. all 6 hits), 6 recent -> rate 10/30d, growth 0.6: not rising
    # Cozy + Farming: 15 prior (3 hits, all tiny), 9 recent -> growth 1.8 (rising, hit share 0.2)
    for i in range(15):
        add(con, 60 + i, [2, 3], reviews=600 if i < 3 else 20)
    for i in range(9):
        add(con, 2 + i, [2, 3], reviews=5)
    for i in range(30):
        add(con, 40 + i, [1, 2], reviews=900 if i < 6 else 10, tiny=i < 20)
    for i in range(6):
        add(con, 3 + i, [1, 2], reviews=1)
    # noise that must be ignored
    add(con, 50, [3, 4], reviews=999, coming_soon=1)
    add(con, 50, [3, 4], reviews=999, typ="dlc")
    add(con, 200, [3, 4], reviews=999)
    con.commit()
    return con


class TablesTests(unittest.TestCase):
    def test_windows_minimums_and_rankings(self):
        t = market_map.compute(world(), TODAY)
        self.assertEqual(t["pairs"], 2)  # Zeta pair is below 15, ineligible games ignored
        rising = {r["pair"]: r for r in t["rising"]}
        self.assertEqual(list(rising), ["Cozy + Farming"])
        r = rising["Cozy + Farming"]
        self.assertEqual((r["recent_releases"], r["prior_releases_per_30d"], r["growth"]), (9, 5.0, 1.8))
        self.assertEqual((r["prior_hit_share"], r["prior_median_reviews"]), (0.2, 20))
        # hit share 0.2 is below the pair median (0.2 and 0.2 -> equal): not below, so not saturating
        self.assertEqual(t["median_hit_share"], 0.2)
        self.assertEqual(t["saturating"], [])

    def test_small_team_ranking_and_saturating(self):
        con = world()
        t = market_map.compute(con, TODAY)
        order = [r["pair"] for r in t["small_team_odds"]]
        self.assertEqual(order, ["Cozy + Roguelike", "Cozy + Farming"])
        self.assertEqual(t["small_team_odds"][0]["prior_tiny_games"], 20)
        self.assertEqual(t["small_team_odds"][0]["tiny_hit_share"], 0.3)
        # lower Farming's hit share so it is growing and below the median
        con.execute("UPDATE apps SET store_reviews=1 WHERE store_reviews=600")
        t = market_map.compute(con, TODAY)
        self.assertEqual([r["pair"] for r in t["saturating"]], ["Cozy + Farming"])


def table_fixture():
    return market_map.compute(world(), TODAY)


class WriteUpTests(unittest.TestCase):
    def llm(self, answer):
        self.con = world()
        self.client = FakeAnthropic({M: lambda kw: Msg(answer)})
        return Llm(self.client, self.con, run_cap=100, monthly_cap=100)

    def test_bad_claims_drop_bullets(self):
        good = {"text": "Farming grows", "claims": [{"pair": "Cozy + Farming", "value": 1.8}]}
        pct = {"text": "20% hit", "claims": [{"pair": "Cozy + Farming", "value": 20}]}
        wrong_val = {"text": "x", "claims": [{"pair": "Cozy + Farming", "value": 7.7}]}
        wrong_pair = {"text": "y", "claims": [{"pair": "Nope + Nada", "value": 1.8}]}
        none = {"text": "z", "claims": []}
        mixed = {"text": "m", "claims": [{"pair": "Cozy + Farming", "value": 1.8},
                                         {"pair": "Cozy + Farming", "value": 99}]}
        llm = self.llm({"headline": "H", "sections": [
            {"title": "Rising", "bullets": [good, pct, wrong_val, wrong_pair, none, mixed]},
            {"title": "Empty", "bullets": [wrong_val]}]})
        out = market_map.write_up(llm, table_fixture(), "brief")
        self.assertEqual(out[0], {"headline": "H"})
        self.assertEqual(len(out), 2)
        self.assertEqual([b["text"] for b in out[1]["bullets"]], ["Farming grows", "20% hit"])
        self.assertEqual(self.client.calls[0]["model"], M)


class CliTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.path, self.out = os.path.join(d, "r.db"), os.path.join(d, "out")

    def test_no_llm_writes_tables(self):
        con = db.connect(self.path)
        _fill(con)
        con.close()
        self.assertEqual(market_map.main(["--db", self.path, "--out", self.out, "--no-llm",
                                          "--today", "2026-12-01"]), 0)
        txt = open(os.path.join(self.out, "2026-12.md"), encoding="utf-8").read()
        self.assertIn("# Market map 2026-12", txt)
        self.assertIn("Cozy + Farming", txt)

    def test_llm_error_writes_banner_and_exits_1(self):
        con = db.connect(self.path)
        _fill(con)
        con.close()
        orig = market_map.Llm
        client = FakeAnthropic({M: lambda kw: RuntimeError("boom")})
        market_map.Llm = lambda con, run_cap: orig(client, con, run_cap=run_cap)
        try:
            code = market_map.main(["--db", self.path, "--out", self.out, "--today", "2026-12-01"])
        finally:
            market_map.Llm = orig
        self.assertEqual(code, 1)
        txt = open(os.path.join(self.out, "2026-12.md"), encoding="utf-8").read()
        self.assertIn("tables only", txt)
        self.assertIn("Cozy + Farming", txt)


def _fill(con):
    src = world()
    for t in ("tags", "apps", "app_tags"):
        rows = src.execute(f"SELECT * FROM {t}").fetchall()
        for r in rows:
            con.execute(f"INSERT INTO {t} VALUES({','.join('?' * len(r))})", tuple(r))
    con.commit()


if __name__ == "__main__":
    unittest.main()
