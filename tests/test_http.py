import unittest
from unittest import mock

import requests

from radar.http import CallBudgetExceeded, Client, HttpError, redact


class Resp:
    def __init__(self, status: int, body: object = None, headers: dict | None = None) -> None:
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._body


def make(responses, limit=90_000):
    sleeps: list[float] = []
    sess = mock.Mock(spec=requests.Session)
    sess.headers = {}
    sess.request.side_effect = responses
    t = [0.0]
    return Client(limit, sleep=sleeps.append, clock=lambda: t[0], session=sess), sleeps, sess


class HttpTests(unittest.TestCase):
    def test_redact(self):
        self.assertNotIn("SECRET", redact("https://x/y?key=SECRET&a=1"))
        self.assertIn("a=1", redact("https://x/y?key=SECRET&a=1"))

    def test_user_agent(self):
        c, _, sess = make([])
        self.assertEqual(sess.headers["User-Agent"], "steam-radar/0.1 (personal research)")

    def test_retry_429_honours_retry_after(self):
        c, sleeps, _ = make([Resp(429, headers={"Retry-After": "7"}), Resp(200, {"ok": 1})])
        self.assertEqual(c.get_json("https://api.steampowered.com/x"), {"ok": 1})
        self.assertIn(7.0, sleeps)

    def test_retries_exhausted(self):
        c, _, sess = make([Resp(503)] * 10)
        with self.assertRaises(HttpError):
            c.get_json("https://api.steampowered.com/x")
        self.assertEqual(sess.request.call_count, 6)

    def test_timeout_retried(self):
        c, _, _ = make([requests.Timeout("key=SECRET"), Resp(200, {"a": 1})])
        self.assertEqual(c.get_json("https://id.twitch.tv/x"), {"a": 1})

    def test_4xx_not_retried_and_status_kept(self):
        c, _, sess = make([Resp(403)])
        with self.assertRaises(HttpError) as cm:
            c.get_json("https://api.steampowered.com/x?key=SECRET")
        self.assertEqual(cm.exception.status, 403)
        self.assertNotIn("SECRET", str(cm.exception))
        self.assertEqual(sess.request.call_count, 1)

    def test_budget_refuses(self):
        c, _, sess = make([Resp(200, {})] * 5, limit=2)
        c.get_json("https://api.steampowered.com/x")
        c.get_json("https://api.steampowered.com/x")
        with self.assertRaises(CallBudgetExceeded):
            c.get_json("https://api.steampowered.com/x")
        self.assertEqual(sess.request.call_count, 2)

    def test_twitch_not_counted_against_steam_budget(self):
        c, _, _ = make([Resp(200, {})] * 3, limit=0)
        c.get_json("https://api.twitch.tv/helix/games")
        self.assertEqual(c.steam_calls, 0)

    def test_host_spacing(self):
        c, sleeps, _ = make([Resp(200, {})] * 2)
        c.get_json("https://store.steampowered.com/a")
        c.get_json("https://store.steampowered.com/b")
        self.assertTrue(any(abs(s - 1.5) < 1e-9 for s in sleeps))


if __name__ == "__main__":
    unittest.main()
