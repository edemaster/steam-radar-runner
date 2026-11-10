import os
import tempfile
import unittest
from datetime import date
from unittest import mock

from radar import backfill, db

from .fakes import FakeClient

TODAY = date(2026, 11, 10)


def routes(totals):
    """GetAppReviews: cumulative total grows with the range end; `totals` maps appid -> total now."""
    def reviews(p):
        total = totals[p["appid"]]
        if "date_range_end" in p:
            total = total // 2
        return {"response": {"query_summary": {"total_reviews": total, "total_positive": total * 9 // 10}}}
    return {"GetTagList": lambda p: {"response": {"tags": []}},
            "GetItems": lambda p: {"response": {"store_items": []}}, "GetAppReviews": reviews}


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "t.db")
        con = db.connect(self.path)
        for appid, rel in ((1, "2026-11-01"), (2, "2026-08-01"), (3, "2026-10-01"), (4, "2025-12-01"),
                           (5, "2026-11-10")):
            con.execute("INSERT INTO apps(appid, name, type, release_date, coming_soon, meta_updated) "
                        "VALUES(?,?,?,?,0,'2026-11-09')", (appid, f"g{appid}", "game", rel))
        con.commit()
        con.close()

    def go(self, client, **kw):
        with mock.patch.dict(os.environ, {}, clear=True):
            return backfill.run(self.path, client=client, today=TODAY, **kw)

    def test_planned_dates(self):
        young = backfill.planned_dates(date(2026, 11, 1), TODAY)
        self.assertEqual(young, [date(2026, 11, 8), TODAY])
        mid = backfill.planned_dates(date(2026, 8, 2), TODAY)  # 100 days: offsets only, <= today
        self.assertEqual([(d - date(2026, 8, 2)).days for d in mid[:-1]], [7, 14, 30, 60, 90])
        self.assertEqual(mid[-1], TODAY)
        weekly = backfill.planned_dates(date(2026, 9, 11), TODAY)  # 60 days old: every 7 days
        self.assertEqual(len(weekly), 8 + 1 + 1)  # weeks 1-8, day 30, today (= day 60)
        self.assertIn(date(2026, 9, 18), weekly)
        self.assertNotIn(date(2026, 9, 12), weekly)

    def test_thin_games_get_one_point_and_rerun_is_free(self):
        c = FakeClient(routes({1: 10, 2: 400, 3: 800, 4: 60, 5: 999}))
        self.go(c)
        con = db.connect(self.path)
        pts = lambda a: [r[0] for r in con.execute("SELECT upto FROM review_points WHERE appid=? ORDER BY upto", (a,))]
        self.assertEqual(pts(1), ["2026-11-10"])  # < 50 reviews: just now
        self.assertEqual(pts(5), [])  # released today: outside 1..455 days
        self.assertIn("2026-08-08", pts(2))
        self.assertEqual(pts(2)[-1], "2026-11-10")
        self.assertEqual(len(pts(3)), 7)  # 40 days: weeks 1-5, day 30, today
        self.assertEqual(len(pts(4)), 7)  # 344 days: offsets 7..180 (365 is in the future), today
        # the ranged call carries both ends; the now call none
        ranged = [p for u, p in c.calls if "GetAppReviews" in u and "date_range_end" in p]
        self.assertTrue(all(p["date_range_start"] == 1 for p in ranged))
        n = len(c.calls)
        self.go(c)
        self.assertEqual(len(c.calls), n + 1)  # only GetTagList is called again: nothing to redo

    def test_newest_first_and_time_stop(self):
        ticks = iter(range(0, 10_000, 60))  # one minute per clock() read
        c = FakeClient(routes({1: 10, 2: 400, 3: 800, 4: 60, 5: 999}))
        self.go(c, max_minutes=3, clock=lambda: next(ticks))
        con = db.connect(self.path)
        done = [r[0] for r in con.execute("SELECT DISTINCT appid FROM review_points ORDER BY appid")]
        self.assertTrue(set(done) < {1, 2, 3, 4})
        self.assertIn(1, done)  # newest release (2026-11-01) goes first
        self.assertNotIn(4, done)  # oldest is left for the next run

    def test_call_budget_stop(self):
        c = FakeClient(routes({1: 10, 2: 400, 3: 800, 4: 60, 5: 999}))
        c.steam_call_limit = backfill.config.BACKFILL_CALL_RESERVE  # reserve already reached
        self.go(c)
        con = db.connect(self.path)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM review_points").fetchone()[0], 0)


    def test_small_old_game_is_skipped_big_one_is_not(self):
        """Old games (past the candidate window) with a small store review count get no history calls."""
        from datetime import datetime, timezone

        def item(appid, rel, count):
            ts = int(datetime.fromisoformat(rel).replace(tzinfo=timezone.utc).timestamp())
            return {"id": appid, "success": 1, "name": f"g{appid}", "type": 0,
                    "release": {"steam_release_date": ts},
                    "reviews": {"summary_filtered": {"review_count": count, "percent_positive": 90}}}

        r = routes({1: 10, 2: 6000, 3: 800, 4: 300, 5: 999})
        r["GetItems"] = lambda p: {"response": {"store_items": [
            item(2, "2026-08-01", 5000), item(4, "2025-12-01", 300)]}}
        c = FakeClient(r)
        self.go(c)
        reviewed = {p["appid"] for u, p in c.calls if "GetAppReviews" in u}
        self.assertNotIn(4, reviewed)  # 344 days old, 300 store reviews: can never be an old hit
        self.assertIn(2, reviewed)  # 101 days old, 5000 store reviews: full history
        self.assertIn(3, reviewed)  # inside the candidate window: never skipped


if __name__ == "__main__":
    unittest.main()
