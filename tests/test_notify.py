import json
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


JARGON = ("skeptic", "signal", "scouted", "unvetted", "candidate", "vetted", "llm")


def every_push():
    import tempfile
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "signals_2026-11-12.json"), "w", encoding="utf-8") as f:
        json.dump({"signals": [{"members": [{"role": "old_hit", "name": "Old"},
                                            {"role": "candidate", "name": "Feed The Forest"}]}]}, f)
    ok_log = "INFO run done: new_apps=12 meta=40 daily=5667 steam_calls=11539 errors=0 x"
    md = "4 signal(s) found, 4 scouted, 4 valid card(s), 0 kept, 4 killed. API spend this run $0.29, this month $0.29."
    return [
        notify.daily_message(ok_log, "  candidates_after_unusual: 32\n  signals: 1\n", d),
        notify.daily_message(ok_log, "  signals: 0\n", None),
        notify.daily_message("new_apps=1 meta=0 daily=3 steam_calls=4 errors=2", "", None),
        notify.daily_message("", "", None),
        notify.daily_failed(),
        notify.weekly_message("digest: week=w kept=2 killed=8 exit=0", md)[0],
        notify.weekly_message("digest: week=w kept=0 killed=4 exit=0", md)[0],
        notify.weekly_message("digest: week=w kept=0 killed=0 exit=0", "")[0],
        notify.weekly_message("digest: week=w kept=0 killed=0 exit=1", "")[0],
        notify.weekly_message("")[0],
        notify.monthly_message("market_map: month=2026-11 pairs=1234 bullets=6 spend=$0.10", "2026-11", True),
        notify.monthly_message("", "2026-11", False),
        notify.test_message(),
    ]


class MessageTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"GITHUB_EVENT_NAME": "schedule"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_push_names_its_job_and_says_what_to_do(self):
        # The first pushes (2026-11-11) said "Skeptic", "unvetted" and "Sunday digest" at 1 am on a Wednesday.
        for p in every_push():
            self.assertTrue(p.title.startswith("Steam radar: "), p.title)
            self.assertTrue(p.title.isascii(), p.title)
            low = (p.title + " " + p.body).lower()
            for word in JARGON:
                self.assertNotIn(word, low, p.title)
            self.assertTrue("nothing to do" in low or "ask claude" in low or "tap" in low, p.body)
            if p.priority == "high":
                self.assertRegex(p.title, "FAILED|incomplete")

    def test_daily_message(self):
        p = every_push()[0]
        self.assertEqual(p.title, "Steam radar: Daily check OK")
        self.assertIn("saved Steam numbers for 5,667 games and found 12 newly listed ones", p.body)
        self.assertIn("1 game(s) match what the radar looks for", p.body)
        self.assertIn(": Feed The Forest.", p.body)
        self.assertIn("No game matches", every_push()[1].body)
        self.assertIn("2 Steam request(s) failed", every_push()[2].body)
        self.assertEqual(every_push()[3].priority, "high")

    def test_weekly_message(self):
        kept, none, empty, cut, crash = every_push()[5:10]
        self.assertEqual(kept.title, "Steam radar: 2 new game idea(s) this week")
        self.assertIn("8 other(s) were rejected", kept.body)
        self.assertIn("API cost $0.29.", none.body)
        self.assertEqual(none.title, "Steam radar: No new game idea this week")
        self.assertIn("nothing to turn into an idea", empty.body)
        self.assertEqual(cut.title, "Steam radar: Weekly ideas incomplete")
        self.assertEqual(crash.title, "Steam radar: Weekly ideas FAILED")
        self.assertFalse(notify.weekly_message("digest: week=w kept=1 killed=0 exit=1")[1])

    def test_monthly_message(self):
        ok, bad = every_push()[10:12]
        self.assertIn("1,234 tag combinations", ok.body)
        self.assertEqual(bad.title, "Steam radar: Monthly market map FAILED")

    def test_manual_run_says_so(self):
        with mock.patch.dict(os.environ, {"GITHUB_EVENT_NAME": "workflow_dispatch"}):
            p = notify.daily_failed()
        self.assertTrue(p.title.endswith("(manual run)"))
        self.assertTrue(p.body.startswith("Started by hand, not by the schedule."))
        self.assertNotIn("manual", notify.daily_failed().title)
        with mock.patch.dict(os.environ, {"GITHUB_EVENT_NAME": "workflow_dispatch", "RADAR_SOURCE": "timer"}):
            self.assertNotIn("manual", notify.daily_failed().title)

    def test_cli_sends_the_push(self):
        seen = {}
        with mock.patch.object(notify, "send", lambda body, title, *a: seen.update(t=title, b=body) or True):
            notify.main(["test"])
        self.assertEqual(seen["t"], "Steam radar: Test message")


if __name__ == "__main__":
    unittest.main()
