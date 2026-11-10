import unittest
from datetime import datetime, timedelta, timezone

from radar import db
from radar.twitch import Twitch, purge_old, snapshot

from .fakes import FakeClient

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def client(games, pages):
    pages = list(pages)
    return FakeClient({
        "oauth2/token": lambda p: {"access_token": "T"},
        "helix/games": lambda p: {"data": games},
        "helix/streams": lambda p: pages.pop(0),
    })


class TwitchTests(unittest.TestCase):
    def setUp(self):
        self.con = db.connect(":memory:")
        self.con.execute("INSERT INTO apps(appid, name) VALUES(1,'Indie One')")

    def test_purge_24h(self):
        old = (NOW - timedelta(hours=25)).isoformat(timespec="seconds")
        new = (NOW - timedelta(hours=1)).isoformat(timespec="seconds")
        self.con.executemany("INSERT INTO twitch_live VALUES(?,?,?,?,?)", [(1, "g", old, 1, 1), (2, "g", new, 1, 1)])
        self.assertEqual(purge_old(self.con, NOW), 1)
        self.assertEqual(self.con.execute("SELECT appid FROM twitch_live").fetchall()[0][0], 2)

    def test_snapshot_sums_pages_and_exact_match(self):
        c = client([{"id": "77", "name": "indie one"}],
                   [{"data": [{"viewer_count": 5}, {"viewer_count": 7}], "pagination": {"cursor": "x"}},
                    {"data": [{"viewer_count": 3}], "pagination": {}}])
        written, errors = snapshot(self.con, Twitch(c, "id", "sec"), [1], NOW)
        self.assertEqual((written, errors), (1, 0))
        row = self.con.execute("SELECT * FROM twitch_live").fetchone()
        self.assertEqual((row["channels"], row["viewers"], row["twitch_game_id"]), (3, 15, "77"))
        g = self.con.execute("SELECT * FROM twitch_games").fetchone()
        self.assertEqual((g["twitch_game_id"], g["match_method"]), ("77", "exact_name"))

    def test_no_match_recorded_and_skipped(self):
        c = client([{"id": "9", "name": "Something Else"}], [])
        written, _ = snapshot(self.con, Twitch(c, "id", "sec"), [1], NOW)
        self.assertEqual(written, 0)
        g = self.con.execute("SELECT * FROM twitch_games").fetchone()
        self.assertEqual((g["twitch_game_id"], g["match_method"]), (None, "none"))

    def test_snapshot_purges_first(self):
        old = (NOW - timedelta(hours=30)).isoformat(timespec="seconds")
        self.con.execute("INSERT INTO twitch_live VALUES(5,'g',?,1,1)", (old,))
        snapshot(self.con, Twitch(client([], []), "id", "sec"), [], NOW)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM twitch_live").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
