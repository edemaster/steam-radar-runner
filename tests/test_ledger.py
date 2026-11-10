import os
import tempfile
import unittest
from datetime import date

from radar.ledger import Ledger


def cluster(fp, cands, olds=(900,)):
    return {"fingerprint": fp, "members": [{"appid": a, "role": "candidate"} for a in cands]
            + [{"appid": a, "role": "old_hit"} for a in olds]}


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "ledger.jsonl")

    def make(self, today="2026-11-10"):
        return Ledger(self.path, date.fromisoformat(today))

    def test_fingerprint_seen_and_persisted(self):
        lg = self.make()
        self.assertFalse(lg.seen(cluster("f1", [1, 2])))
        lg.add("2026-11-10", cluster("f1", [1, 2]), "T", "kill", "why")
        lg.save()
        lg2 = self.make()
        self.assertTrue(lg2.seen(cluster("f1", [7])))
        self.assertEqual(lg2.recent_titles(), ["T"])

    def test_candidate_subset_seen(self):
        lg = self.make()
        lg.add("2026-11-03", cluster("f1", [1, 2, 3]), "T", "keep", "why")
        self.assertTrue(lg.seen(cluster("f2", [1, 3])))
        self.assertFalse(lg.seen(cluster("f3", [1, 4])))

    def test_lookback_expiry(self):
        lg = self.make("2026-11-10")
        lg.add("2025-01-06", cluster("f1", [1, 2]), "T", "kill", "why")  # far older than 26 weeks
        self.assertFalse(lg.seen(cluster("f2", [1, 2])))
        lg.add("2026-08-03", cluster("f9", [5]), "U", "kill", "why")  # about 14 weeks ago
        self.assertTrue(lg.seen(cluster("f10", [5])))
