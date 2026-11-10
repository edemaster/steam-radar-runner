import json
import math
import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta

from radar import collect, config, db, detect, steam

from .fakes import FakeClient

WEEK = date(2026, 11, 10)
RARE = [(10, 100), (11, 90), (12, 80)]
COMMON = [(1, 100), (2, 90), (3, 80)]
OTHER = [(20, 100), (21, 90), (22, 80)]
# cumulative (total, positive) at N days before WEEK
STEADY = {14: (290, 260), 7: (340, 306), 0: (400, 360)}


def put(con, appid, age=30, tags=RARE, pts=None, desc="cozy farming robot adventure", **kw):
    """Insert one game that passes every candidate rule unless overridden."""
    row = {"name": f"g{appid}", "type": "game", "coming_soon": 0, "developers": '["Dev%d"]' % appid,
           "publishers": '["Dev%d"]' % appid, "self_published": 1, "dev_app_count": 1, "pub_app_count": 1,
           "price_cents": 999, "is_free": 0, "short_desc": desc, "meta_updated": "2026-11-09"}
    row.update(kw)
    row["release_date"] = (WEEK - timedelta(days=age)).isoformat()
    row["appid"] = appid
    con.execute(f"INSERT INTO apps({','.join(row)}) VALUES({','.join('?' * len(row))})", list(row.values()))
    con.executemany("INSERT INTO app_tags VALUES(?,?,?)", [(appid, t, w) for t, w in tags])
    for day, (t, p) in (STEADY if pts is None else pts).items():
        d = day if isinstance(day, date) else WEEK - timedelta(days=day)
        con.execute("INSERT INTO review_points VALUES(?,?,?,?)", (appid, d.isoformat(), t, p))


def world(path):
    con = db.connect(path)
    con.executemany("INSERT INTO tags VALUES(?,?)", [(1, "Action"), (2, "Casual"), (3, "Indie"), (10, "Farming"),
                                                    (11, "Robots"), (12, "Cozy"), (20, "X"), (21, "Y"),
                                                    (22, "Z")])
    for i in range(500):  # filler games make the rare tag pair rare (share < 1 %)
        put(con, 5000 + i, age=800, tags=COMMON, pts={0: (10, 9)}, desc=f"filler shooter {i}")
    put(con, 900, age=200, pts={0: (3000, 2800)})  # old hit
    put(con, 901)  # steady candidate: w1=60 >= w0=50
    put(con, 902, tags=OTHER, dev_app_count=5)
    put(con, 903, tags=OTHER, price_cents=3000)
    put(con, 904, tags=OTHER, pts={14: (290, 150), 7: (340, 180), 0: (400, 200)})  # 50 % positive
    put(con, 905, tags=OTHER, pts={14: (290, 260), 7: (350, 300), 0: (390, 340)})  # w1=40
    put(con, 906, tags=OTHER, pts={14: (200, 180), 7: (280, 250), 0: (340, 300)})  # w1=60 < w0=80
    put(con, 907, tags=OTHER, pts={14: (20, 19), 7: (40, 38), 0: (90, 80)})  # 90 reviews
    rel = WEEK - timedelta(days=10)
    wk1 = rel + timedelta(days=7)
    put(con, 908, age=10, pts={wk1: (250, 230), WEEK: (300, 280)})  # young: 250 in week one
    put(con, 909, age=10, tags=OTHER, pts={wk1: (150, 140), WEEK: (300, 280)})  # only 150 in week one
    put(con, 910, tags=COMMON)  # passes everything but the unusual-pair rule
    put(con, 911, age=200, tags=OTHER, pts={0: (1500, 1400)})  # old, too few reviews
    put(con, 950, age=-60, tags=RARE, coming_soon=1, pts={}, name="Clone")  # coming soon, similar
    con.commit()
    return con


class DetectTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "t.db")
        self.con = world(self.path)

    def games(self):
        return {g.appid: g for g in detect.load_games(self.con, WEEK)}

    def test_each_rule_fails_exactly_its_game(self):
        g = self.games()
        self.assertEqual((g[900].fails, g[900].role), ([], "old_hit"))
        self.assertEqual(g[901].fails, [])
        self.assertEqual((g[901].w1, g[901].w0), (60, 50))
        self.assertEqual(g[902].fails, ["dev_count"])
        self.assertEqual(g[903].fails, ["price"])
        self.assertEqual(g[904].fails, ["positive_share"])
        self.assertEqual(g[905].fails, ["growth"])
        self.assertEqual(g[906].fails, ["growth"])
        self.assertEqual(g[907].fails, ["reviews"])
        self.assertEqual(g[908].fails, [])
        self.assertEqual((g[908].w1, g[908].w0), (250, 0))
        self.assertEqual(g[909].fails, ["growth"])
        self.assertEqual(g[911].fails, ["reviews"])
        self.assertNotIn(950, g)  # a coming-soon game is never judged

    def test_growth_score(self):
        self.assertAlmostEqual(self.games()[901].growth, math.log2(61 / 51) + math.log10(61))

    def test_age_windows(self):
        put(self.con, 930, age=6, tags=OTHER, pts={0: (400, 380)})
        put(self.con, 931, age=91, tags=OTHER, pts={0: (3000, 2800)})
        put(self.con, 932, age=90, tags=OTHER, pts={14: (290, 260), 7: (340, 306), 0: (400, 360)})
        put(self.con, 933, age=456, tags=OTHER, pts={0: (3000, 2800)})
        g = self.games()
        self.assertEqual(g[930].fails, ["too_young"])
        self.assertEqual(g[931].role, "old_hit")
        self.assertEqual(g[931].fails, [])
        self.assertEqual((g[932].role, g[932].fails), ("candidate", []))  # day 90 is a candidate, never both
        self.assertNotIn(933, g)  # older than OLD_HIT_MAX_DAYS

    def test_tiny_publisher_rule(self):
        put(self.con, 920, publishers='["Big"]', self_published=0, pub_app_count=11, tags=OTHER)
        put(self.con, 921, publishers='["Big"]', self_published=0, pub_app_count=10, tags=OTHER)
        put(self.con, 922, is_free=1, price_cents=0, tags=OTHER)
        g = self.games()
        self.assertEqual(g[920].fails, ["publisher"])
        self.assertEqual(g[921].fails, [])
        self.assertEqual(g[922].fails, [])

    def test_ccu_trend_needs_ten_points(self):
        for i in range(9):
            self.con.execute("INSERT INTO daily VALUES(901, ?, ?, NULL, NULL)",
                             ((WEEK - timedelta(days=i)).isoformat(), 100))
        self.assertEqual(detect.ccu_trend(self.con, 901, WEEK), (100, None))
        self.con.execute("INSERT INTO daily VALUES(901, ?, 40, NULL, NULL)", ((WEEK - timedelta(days=9)).isoformat(),))
        latest, trend = detect.ccu_trend(self.con, 901, WEEK)
        self.assertEqual((latest, trend), (100, 100 / 100))

    def test_signal_end_to_end(self):
        rep: dict = {}
        sample = {"reviews": [{"author": {"playtime_forever": 10}, "received_for_free": True, "language": "english"},
                              {"author": {"playtime_forever": 500}, "language": "german"}]}
        c = FakeClient({"GetAppReviews": lambda p: {"response": sample}})
        docs = detect.detect(self.con, WEEK, c, rep)
        self.assertEqual(len(docs), 1)
        d = docs[0]
        by = {m["appid"]: m for m in d["members"]}
        self.assertEqual(sorted(by), [900, 901, 908])  # 910 fails unusual; 902-909 fail a rule
        self.assertEqual(by[900]["role"], "old_hit")
        self.assertEqual(by[901]["store_url"], "https://store.steampowered.com/app/901/")
        self.assertEqual(by[901]["unusual_pairs"][0]["games_with_both"], 4)  # 900, 901, 908 and the clone
        self.assertEqual(sorted(d["shared_tags"]), ["Cozy", "Farming", "Robots"])
        self.assertEqual(by[901]["review_sample"]["share_short_play"], 0.5)
        self.assertEqual(by[901]["red_flags"], ["share_free 0.50 >= 0.20", "share_short_play 0.50 >= 0.50"])
        self.assertNotIn("red_flags", by[900])
        self.assertEqual(by[901]["short_desc"], "cozy farming robot adventure")
        self.assertEqual(by[901]["tags"], ["Farming", "Robots", "Cozy"])
        self.assertEqual(by[901]["positive_share"], 0.9)
        self.assertEqual(detect.red_flags({"n": 60, "share_free": 0, "share_short_play": 0, "share_few_games": None,
                                           "languages": 2}), ["languages 2 <= 2 with n=60"])
        self.assertEqual(d["clone_pressure"]["count"], 1)
        self.assertEqual(d["clone_pressure"]["top"][0]["appid"], 950)
        self.assertEqual(len(c.calls), 2)  # one sample per candidate, none for the old hit
        row = self.con.execute("SELECT * FROM review_samples WHERE appid=901").fetchone()
        self.assertEqual((row["day"], row["n"], row["share_free"], row["languages"]), ("2026-11-10", 2, 0.5, 2))
        self.assertEqual(rep["old_hits"], 1)
        self.assertEqual((rep["candidates_before_unusual"], rep["candidates_after_unusual"]), (3, 2))
        self.assertEqual(rep["nearest_misses"]["candidate:unusual"][0]["appid"], 910)
        self.assertIn("candidate:dev_count", rep["nearest_misses"])
        sig = self.con.execute("SELECT * FROM signals").fetchall()
        self.assertEqual(len(sig), 1)
        self.assertEqual(json.loads(sig[0]["appids"]), [900, 901, 908])
        self.assertEqual(json.loads(sig[0]["roles"])["900"], "old_hit")

    def test_rerun_replaces_week_and_fingerprint_is_stable(self):
        a = detect.detect(self.con, WEEK, None)[0]["fingerprint"]
        b = detect.detect(self.con, WEEK, None)[0]["fingerprint"]
        self.assertEqual(a, b)
        self.assertEqual(len(a), 12)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM signals").fetchone()[0], 1)
        self.assertEqual(detect.fingerprint([3, 1, 2]), detect.fingerprint([1, 2, 3]))
        self.assertNotEqual(detect.fingerprint([1, 2]), detect.fingerprint([1, 2, 3]))

    def test_watch_sync_drops_stale_old_hits_only(self):
        self.con.execute("INSERT INTO watch VALUES(7, 'old_hit')")
        self.con.execute("INSERT INTO watch VALUES(8, 'manual')")
        detect.detect(self.con, WEEK, None)
        self.assertEqual([tuple(r) for r in self.con.execute("SELECT appid, reason FROM watch ORDER BY appid")],
                         [(8, "manual"), (900, "old_hit")])

    def test_out_file(self):
        out = os.path.join(tempfile.mkdtemp(), "out")
        self.con.close()
        detect.run(self.path, WEEK, report=False, out=out, client=FakeClient({}))
        with open(os.path.join(out, "signals_2026-11-10.json"), encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)["signals"]), 1)


class Stub:
    def __init__(self, appid, role, growth=1.0, devs=()):
        self.appid, self.role, self.growth, self.devs = appid, role, growth, frozenset(devs)


class ClusterTests(unittest.TestCase):
    def test_components(self):
        self.assertEqual(detect.components([1, 2, 3, 4, 5], [(1, 2), (2, 3)]), [[1, 2, 3], [4], [5]])

    def test_signal_needs_both_roles_and_similarity(self):
        high = lambda a, b: 0.9
        self.assertEqual(detect.build_signals([Stub(1, "candidate"), Stub(2, "candidate")], [], high), [])
        self.assertEqual(len(detect.build_signals([Stub(1, "candidate")], [Stub(2, "old_hit")], high)), 1)
        self.assertEqual(detect.build_signals([Stub(1, "candidate")], [Stub(2, "old_hit")], lambda a, b: 0.1), [])

    def test_same_developer_is_a_sequel_not_a_signal(self):
        high = lambda a, b: 0.9
        self.assertEqual(detect.build_signals([Stub(1, "candidate", devs=["Joe"])], [Stub(2, "old_hit", devs=["Joe", "X"])], high), [])
        self.assertEqual(len(detect.build_signals([Stub(1, "candidate", devs=["Joe"])], [Stub(2, "old_hit", devs=["Ann"])], high)), 1)

    def test_oversized_component_keeps_best_eight_and_both_roles(self):
        cands = [Stub(i, "candidate") for i in range(1, 10)]
        sim = lambda a, b: config.SIM_MIN + 0.01 if 100 in (a, b) else 0.9  # the old hit is everyone's weakest link
        sig = detect.build_signals(cands, [Stub(100, "old_hit")], sim)[0]
        self.assertEqual(len(sig["appids"]), config.MAX_CLUSTER)
        self.assertIn(100, sig["appids"])

    def test_clone_pressure_counts_near_look_alikes_only(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript("CREATE TABLE apps(appid INT, name TEXT, release_date TEXT, coming_soon INT, type TEXT);"
                          "CREATE TABLE app_tags(appid INT);")
        for a in (11, 12, 13):
            con.execute("INSERT INTO apps VALUES(?,?,?,1,'game')", (a, f"c{a}", "2027-01-01"))
            con.execute("INSERT INTO app_tags VALUES(?)", (a,))
        sims = {11: config.SIM_MIN + 0.01, 12: config.CLONE_SIM_MIN, 13: 0.7}  # 11 is a loose match only
        cp = detect.clone_pressure(con, lambda a, b: sims[a], [1])
        self.assertEqual(cp["count"], 2)
        self.assertEqual([c["appid"] for c in cp["top"]], [13, 12])

    def test_score(self):
        sig = detect.build_signals([Stub(1, "candidate", 3.0)], [Stub(2, "old_hit")], lambda a, b: 0.6)[0]
        self.assertAlmostEqual(sig["score"], 3.0 + 2 + 0.6)


class SimTests(unittest.TestCase):
    def test_jaccard_cosine_tokens(self):
        self.assertAlmostEqual(detect.weighted_jaccard({1: 1.0, 2: 1.0}, {1: 1.0, 2: 1.0}), 1.0)
        self.assertAlmostEqual(detect.weighted_jaccard({1: 1.0}, {2: 1.0}), 0.0)
        self.assertAlmostEqual(detect.weighted_jaccard({1: 1.0, 2: 1.0}, {1: 1.0}), 0.5)
        self.assertAlmostEqual(detect.cosine({"a": 1.0}, {"a": 1.0}), 1.0)
        self.assertEqual(detect.tokens("The Cozy 2D farm, by us!"), ["cozy", "farm"])


class ReviewSampleTests(unittest.TestCase):
    def test_aggregates_only(self):
        rows = [{"author": {"steamid": "x", "playtime_forever": 5, "num_games_owned": 1}, "language": "english",
                 "received_for_free": True, "review": "secret"},
                {"author": {"playtime_forever": 60, "num_games_owned": 100}, "language": "french"}]
        c = FakeClient({"GetAppReviews": lambda p: {"response": {"reviews": rows}}})
        self.assertEqual(steam.review_sample(c, 1), {"n": 2, "share_short_play": 0.5, "share_few_games": 0.5,
                                                    "share_free": 0.5, "languages": 2})
        self.assertEqual((c.calls[0][1]["num_per_page"], c.calls[0][1]["filter"]), (100, 1))

    def test_missing_games_owned_and_empty(self):
        c = FakeClient({"GetAppReviews": lambda p: {"response": {"reviews": [{"author": {"playtime_forever": 1}}]}}})
        self.assertIsNone(steam.review_sample(c, 1)["share_few_games"])
        c = FakeClient({"GetAppReviews": lambda p: {"response": {"reviews": []}}})
        self.assertIsNone(steam.review_sample(c, 1))


class MigrationTests(unittest.TestCase):
    def test_from_chunk1_schema(self):
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        con = sqlite3.connect(path)
        con.executescript(db.MIGRATIONS[0])
        con.execute("PRAGMA user_version = 1")
        con.execute("INSERT INTO apps(appid, name) VALUES(1, 'keep')")
        con.commit()
        con.close()
        con = db.connect(path)
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], len(db.MIGRATIONS))
        self.assertEqual(tuple(con.execute("SELECT name, pub_app_count FROM apps").fetchone()), ("keep", None))
        for t in ("signals", "review_samples"):
            con.execute(f"SELECT * FROM {t}")
        db.migrate(con)  # idempotent

    def test_pub_app_count(self):
        con = db.connect(os.path.join(tempfile.mkdtemp(), "t.db"))
        for a, pubs in ((1, '["P","Q"]'), (2, '["P"]'), (3, '["Z"]')):
            con.execute("INSERT INTO apps(appid, developers, publishers) VALUES(?,?,?)", (a, pubs, pubs))
        collect.recompute_dev_counts(con)
        self.assertEqual([r[0] for r in con.execute("SELECT pub_app_count FROM apps ORDER BY appid")], [2, 2, 1])


if __name__ == "__main__":
    unittest.main()
