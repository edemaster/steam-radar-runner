import json
import os
import tempfile
import unittest
from datetime import date

from radar import db, followup
from radar.ledger import Ledger, snapshot_of

TODAY = date(2026, 12, 10)


def cl(fp, appids):
    return {"fingerprint": fp, "members": [{"appid": a, "role": "candidate", "reviews_total": 300, "w1": 40}
                                           for a in appids] + [{"appid": 9, "role": "old_hit", "reviews_total": 9}]}


class FollowupTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.led, self.fu = os.path.join(d, "l.jsonl"), os.path.join(d, "f.jsonl")
        self.con = db.connect(os.path.join(d, "t.db"))

    def pts(self, appid, rows):
        for day, t in rows:
            self.con.execute("INSERT INTO review_points VALUES(?,?,?,?)", (appid, day, t, t))

    def judge(self, week, fp, appids, verdict="kill", reason="clones: too many"):
        lg = Ledger(self.led, date(2026, 12, 10))
        lg.add(week, cl(fp, appids), "T" + fp, verdict, reason, snapshot=snapshot_of(cl(fp, appids)))
        lg.save()

    def test_snapshot_builder(self):
        self.assertEqual(snapshot_of(cl("f", [1, 2])),
                         {"1": {"reviews_total": 300, "w1": 40}, "2": {"reviews_total": 300, "w1": 40}})
        lg = Ledger(self.led)
        lg.add("2026-01-01", cl("f", [1]), "t", "kill", "r")
        self.assertNotIn("snapshot", lg.entries[0])

    def test_28_day_check_once(self):
        self.judge("2026-11-10", "f1", [1])
        self.pts(1, [("2026-11-10", 300), ("2026-12-03", 500), ("2026-12-10", 540)])
        new = followup.check(self.con, self.led, self.fu, TODAY)
        self.assertEqual([(r["horizon_days"], r["outcome"]) for r in new], [(28, "held")])
        self.assertEqual(new[0]["reason_check"], "clones")
        self.assertEqual(new[0]["games"]["1"]["w1_now"], 40)
        self.assertEqual(followup.check(self.con, self.led, self.fu, TODAY), [])
        later = followup.check(self.con, self.led, self.fu, date(2027, 1, 7))
        self.assertEqual([r["horizon_days"] for r in later], [56])
        with open(self.fu) as f:
            self.assertEqual(len(f.read().splitlines()), 2)

    def test_not_due_yet(self):
        self.judge("2026-11-20", "f1", [1])
        self.assertEqual(followup.check(self.con, self.led, self.fu, TODAY), [])

    def test_faded_unknown_and_mixed(self):
        self.judge("2026-11-10", "f2", [1])
        self.pts(1, [("2026-12-03", 400), ("2026-12-10", 405)])  # w1 5 < 20
        self.judge("2026-11-10", "f3", [2])  # no series
        self.judge("2026-11-10", "f4", [3, 4])
        self.pts(3, [("2026-12-03", 400), ("2026-12-10", 405)])
        self.pts(4, [("2026-12-03", 400), ("2026-12-10", 450)])
        out = {r["fingerprint"]: r["outcome"] for r in followup.check(self.con, self.led, self.fu, TODAY)}
        self.assertEqual(out, {"f2": "faded", "f3": "unknown", "f4": "held"})

    def test_summary_tally(self):
        rows = [{"fingerprint": f"f{i}", "title": f"t{i}", "verdict": v, "week": "2026-10-01", "reason_check": c,
                 "horizon_days": 56, "outcome": o, "games": {}}
                for i, (v, c, o) in enumerate([("keep", "", "held"), ("kill", "clones", "held"),
                                               ("kill", "clones", "faded"), ("kill", "price", "faded")])]
        with open(self.fu, "w") as f:
            f.write("\n".join(json.dumps(r) for r in rows) + "\n")
        txt = "\n".join(followup.summary_lines(rows[:1], self.fu))
        self.assertIn("## Follow-up", txt)
        self.assertIn("- keep: 1/1 held (100%)", txt)
        self.assertIn("- kill: 1/3 held (33%)", txt)
        self.assertIn("kill by `clones`: 1/2 held (50%)", txt)
        self.assertEqual(followup.summary_lines([], os.path.join(tempfile.mkdtemp(), "none.jsonl")), [])


if __name__ == "__main__":
    unittest.main()
