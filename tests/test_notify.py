import os
import unittest
from unittest import mock

import requests

from radar import notify


class Resp:
    def __init__(self, code):
        self.status_code = code


class NotifyTests(unittest.TestCase):
    def test_no_topic_sends_nothing(self):
        calls = []
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(notify.send("hi", post=lambda *a, **k: calls.append(a)))
        self.assertEqual(calls, [])

    def test_posts_to_topic_with_headers(self):
        seen = {}

        def post(url, data, headers, timeout):
            seen.update(url=url, data=data, headers=headers)
            return Resp(200)
        with mock.patch.dict(os.environ, {"NTFY_TOPIC": "abc123"}, clear=True):
            self.assertTrue(notify.send("Collected 5 games", title="Radar", priority="high",
                                        click="https://example.com/run", tags="x", post=post))
        self.assertEqual(seen["url"], "https://ntfy.sh/abc123")
        self.assertEqual(seen["data"], "Collected 5 games".encode())
        self.assertEqual(seen["headers"], {"Title": "Radar", "Priority": "high",
                                           "Click": "https://example.com/run", "Tags": "x"})

    def test_failures_never_raise(self):
        def boom(*a, **k):
            raise requests.ConnectionError("down")
        self.assertFalse(notify.send("m", topic="t", post=boom))
        self.assertFalse(notify.send("m", topic="t", post=lambda *a, **k: Resp(500)))


class MessageTests(unittest.TestCase):
    def test_daily_message(self):
        import json, tempfile
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "signals_2026-11-12.json"), "w", encoding="utf-8") as f:
            json.dump({"signals": [{"members": [{"role": "old_hit", "name": "Old"}, {"role": "candidate", "name": "Feed The Forest"},
                                                {"role": "candidate", "name": "Mycelarium"}]}]}, f)
        msg = notify.daily_message("INFO run done: new_apps=12 meta=40 daily=5667 steam_calls=11539 errors=0 x",
                                   "detect report\n  candidates_after_unusual: 32\n  signals: 1\n", d)
        self.assertEqual(msg, "Tracked 5,667 games, 12 new on Steam. 32 young small-team games growing fast; "
                              "1 possible signal(s) now: Feed The Forest. Next digest: Sunday morning.")
        self.assertIn("2 errors", notify.daily_message("new_apps=1 meta=0 daily=3 steam_calls=4 errors=2", "", None))
        self.assertTrue(notify.daily_message("", "", None).startswith("Collection finished but printed no summary"))

    def test_weekly_message(self):
        md = "4 signal(s) found, 4 scouted, 4 valid card(s), 0 kept, 4 killed. API spend this run $0.29, this month $0.29."
        msg, ok = notify.weekly_message("x\ndigest: week=2026-11-10 kept=0 killed=4 exit=0\n", md)
        self.assertTrue(ok)
        self.assertEqual(msg, "No concept survived the Skeptic this week, 4 killed (API $0.29). Tap to read the digest.")
        msg, ok = notify.weekly_message("digest: week=2026-11-10 kept=2 killed=1 exit=1")
        self.assertFalse(ok)
        self.assertTrue(msg.startswith("2 concept(s) kept, 1 killed.") and "unvetted" in msg)
        self.assertEqual(notify.weekly_message("")[1], False)


if __name__ == "__main__":
    unittest.main()
