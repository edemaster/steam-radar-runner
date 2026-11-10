import os
import tempfile
import unittest
from datetime import date
from unittest import mock

from radar import collect, db

from .fakes import FakeClient, fixture

TODAY = date(2026, 11, 10)


def routes():
    items = fixture("items.json")
    return {
        "GetTagList": lambda p: {"response": {"tags": [{"tagid": 19, "name": "Action"}]}},
        "GetItems": lambda p: items,
        "GetNumberOfCurrentPlayers": lambda p: {"response": {"player_count": 11, "result": 1}},
        "GetAppReviews": lambda p: {"response": {"query_summary": {"total_reviews": 20, "total_positive": 15}}},
    }


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.db")

    def go(self, client, seed, key=None):
        with mock.patch.dict(os.environ, {"STEAM_API_KEY": key} if key else {}, clear=True):
            return collect.run(self.path, None, seed, client, TODAY)

    def test_end_to_end_without_key(self):
        c = FakeClient(routes())
        rc = self.go(c, [100, 101, 102, 103])
        self.assertEqual(rc, 0)
        con = db.connect(self.path)
        a = con.execute("SELECT * FROM apps WHERE appid=100").fetchone()
        self.assertEqual((a["name"], a["release_date"], a["price_cents"], a["dev_app_count"]), ("Indie One", "2026-10-26", 999, 3))
        self.assertEqual(con.execute("SELECT type FROM apps WHERE appid=103").fetchone()[0], "unavailable")
        self.assertEqual(con.execute("SELECT type FROM apps WHERE appid=102").fetchone()[0], "type_1")
        self.assertEqual(con.execute("SELECT COUNT(*) FROM app_tags WHERE appid=100").fetchone()[0], 2)
        days = con.execute("SELECT appid, ccu, reviews_total, reviews_positive FROM daily").fetchall()
        self.assertEqual([tuple(d) for d in days], [(100, 11, 20, 15)])  # only the released game is tracked
        self.assertEqual(con.execute("SELECT name FROM tags").fetchone()[0], "Action")
        self.assertEqual(con.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)
        self.assertFalse(any("GetAppList" in u for u, _ in c.calls))

    def test_rerun_idempotent_and_watch(self):
        self.go(FakeClient(routes()), [100])
        con = db.connect(self.path)
        con.execute("INSERT INTO watch VALUES(101,'old hit')")
        con.commit()
        con.close()
        self.go(FakeClient(routes()), [100])
        con = db.connect(self.path)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM daily WHERE day='2026-11-10'").fetchone()[0], 2)

    def test_app_list_used_with_key(self):
        r = routes()
        r["GetAppList"] = lambda p: {"response": {"apps": [{"appid": 100, "name": "Indie One"}]}}
        c = FakeClient(r)
        self.go(c, [], key="K")
        self.assertTrue(any("GetAppList" in u for u, _ in c.calls))

    def test_exit_nonzero_when_nothing_collected(self):
        def boom(p):
            from radar.http import HttpError
            raise HttpError("down")
        c = FakeClient({"GetTagList": boom})
        self.assertEqual(self.go(c, []), 1)

    def test_migration_idempotent(self):
        con = db.connect(self.path)
        db.migrate(con)
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], len(db.MIGRATIONS))


if __name__ == "__main__":
    unittest.main()
