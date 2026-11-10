import os
import sqlite3
import tempfile
import unittest
from datetime import date

from radar import db, series


def D(s):
    return date.fromisoformat(s)


class SeriesTests(unittest.TestCase):
    def setUp(self):
        self.con = db.connect(os.path.join(tempfile.mkdtemp(), "t.db"))

    def test_merge_daily_wins_and_sorted(self):
        c = self.con
        c.executemany("INSERT INTO review_points VALUES(1,?,?,?)",
                      [("2026-11-03", 50, 40), ("2026-11-01", 10, 9), ("2026-11-05", 70, 60)])
        c.executemany("INSERT INTO daily(appid, day, ccu, reviews_total, reviews_positive) VALUES(1,?,?,?,?)",
                      [("2026-11-05", 5, 75, 65), ("2026-11-06", None, None, None), ("2026-11-07", 5, 90, 80)])
        self.assertEqual(series.cumulative(c, 1), [(D("2026-11-01"), 10, 9), (D("2026-11-03"), 50, 40),
                                                   (D("2026-11-05"), 75, 65), (D("2026-11-07"), 90, 80)])
        self.assertEqual(series.cumulative(c, 2), [])

    def test_between(self):
        s = [(D("2026-11-01"), 10, 9), (D("2026-11-08"), 50, 40), (D("2026-11-15"), 90, 70)]
        self.assertEqual(series.reviews_between(s, D("2026-11-08"), D("2026-11-15")), 40)
        self.assertEqual(series.reviews_between(s, D("2026-11-10"), D("2026-11-20")), 40)  # last point <=
        self.assertEqual(series.reviews_between(s, D("2026-10-01"), D("2026-11-08")), 50)  # before start: 0
        self.assertIsNone(series.reviews_between(s, D("2026-10-01"), D("2026-10-20")))  # nothing <= d1
        self.assertIsNone(series.reviews_between([], D("2026-10-01"), D("2026-10-20")))
        self.assertEqual(series.reviews_between(s, D("2026-11-15"), D("2026-11-15")), 0)
        self.assertEqual(series.window(s, D("2026-11-15"), 7), 40)
        self.assertIsNone(series.value_at(s, D("2026-10-31")))
        self.assertEqual(series.value_at(s, D("2026-11-01")), s[0])


if __name__ == "__main__":
    unittest.main()
