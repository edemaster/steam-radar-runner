"""Tests for the deeper funnel's parts: review_texts, player_voice, Llm.research, claims `extra`."""
import os
import tempfile
import unittest

from anthropic import types as t

from radar import claims, config, db, steam, voice
from radar.llm import BudgetExceeded, Llm, LlmError, LlmRefusal

from .fakes import FakeAnthropic, FakeClient, Msg, RMsg

SONNET = config.RESEARCH_MODEL


def row(i, up=True, minutes=600, text=None):
    return {"review": text if text is not None else f"review {i} " + "x" * 800, "voted_up": up,
            "author": {"steamid": "7656119", "playtime_forever": minutes, "num_reviews": 3},
            "recommendationid": str(i), "timestamp_created": 5}


class ReviewTextsTests(unittest.TestCase):
    def test_pages_by_cursor_and_keeps_only_text_up_hours(self):
        pages = {"*": {"reviews": [row(i) for i in range(100)], "cursor": "c2"},
                 "c2": {"reviews": [row(i, up=False, minutes=90) for i in range(100, 200)], "cursor": "c3"},
                 "c3": {"reviews": [row(999)], "cursor": "c4"}}
        seen = []

        def h(p):
            seen.append(p)
            return {"response": pages[p["cursor"]]}
        out = steam.review_texts(FakeClient({"GetAppReviews": h}), 5, n=150)
        self.assertEqual(len(out), 150)
        self.assertEqual([p["cursor"] for p in seen], ["*", "c2"])
        self.assertEqual(set(out[0]), {"text", "up", "hours"})
        self.assertEqual(len(out[0]["text"]), config.VOICE_REVIEW_CHARS)
        self.assertEqual((out[0]["up"], out[0]["hours"]), (True, 10.0))
        self.assertEqual((out[120]["up"], out[120]["hours"]), (False, 1.5))
        p = seen[0]
        self.assertEqual((p["languages[0]"], p["filter"], p["day_range"], p["purchase_type"], p["num_per_page"]),
                         ("english", 1, 0, 1, 100))

    def test_stops_on_empty_and_repeated_cursor(self):
        n = []

        def h(p):
            n.append(1)
            return {"response": {"reviews": [row(len(n))], "cursor": "same"}}
        out = steam.review_texts(FakeClient({"GetAppReviews": h}), 5, n=500)
        self.assertEqual(len(out), 2)  # "*" then "same", and "same" repeats
        self.assertEqual(steam.review_texts(FakeClient({"GetAppReviews": lambda p: {"response": {}}}), 5), [])


class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.con = db.connect(os.path.join(tempfile.mkdtemp(), "t.db"))

    def run_voice(self, reviews, out):
        steam_client = FakeClient({"GetAppReviews": lambda p: {"response": {"reviews": reviews}}})
        ai = FakeAnthropic({config.VOICE_MODEL: lambda kw: Msg(out)})
        return voice.player_voice(Llm(ai, self.con), steam_client, 1), ai

    def test_invented_quote_dropped_and_counts_overwritten(self):
        reviews = [row(i, up=i < 8, text=f"Fun game {i}.  Needs  MORE maps please") for i in range(12)]
        item = lambda q: {"point": "p", "mentions": 4, "quote": q}
        out = {"n_read": 999, "n_positive": 999, "audience": "a", "loves": [item("fun game 3. needs more maps")],
               "complaints": [item("This quote was never written")], "wishes": []}
        got, _ = self.run_voice(reviews, out)
        self.assertEqual((got["n_read"], got["n_positive"]), (12, 8))
        self.assertEqual(len(got["loves"]), 1)  # whitespace and case do not matter
        self.assertEqual(got["complaints"], [])

    def test_fewer_than_ten_reviews_is_none_and_no_call(self):
        got, ai = self.run_voice([row(i) for i in range(9)], {})
        self.assertIsNone(got)
        self.assertEqual(ai.calls, [])


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.con = db.connect(os.path.join(tempfile.mkdtemp(), "t.db"))

    def go(self, fn, **kw):
        self.ai = FakeAnthropic({SONNET: fn})
        llm = Llm(self.ai, self.con, **{"run_cap": 100, "monthly_cap": 100, **kw})
        return llm, lambda: llm.research(SONNET, "medium", 6000, "sys", "user text", "research")

    def test_pause_turn_continues_with_assistant_content_and_costs_searches(self):
        search = t.ServerToolUseBlock(type="server_tool_use", id="s1", name="web_search", input={"query": "q"})
        answers = [RMsg("thinking", stop="pause_turn", searches=2, extra_blocks=[search]),
                   RMsg("done\nFINDING: x | URL: https://a.com/p", pages={"https://a.com/p/": "1,200 sold"}, searches=1)]
        llm, call = self.go(lambda kw: answers.pop(0))
        r = call()
        self.assertEqual(len(self.ai.calls), 2)
        second = self.ai.calls[1]["messages"]
        self.assertEqual([m["role"] for m in second], ["user", "assistant"])  # no extra user message
        self.assertEqual(second[1]["content"][0]["type"], "server_tool_use")
        self.assertEqual(second[1]["content"][1], {"type": "text", "text": "thinking"})
        self.assertEqual(r.searches, 3)
        self.assertEqual(r.fetched, {"https://a.com/p": "1,200 sold"})
        self.assertIn("done", r.text)
        kw = self.ai.calls[0]
        self.assertEqual(kw["output_config"], {"effort": "medium"})
        self.assertEqual([x["name"] for x in kw["tools"]], ["web_search", "web_fetch"])
        costs = [x["usd"] for x in self.con.execute("SELECT usd FROM spend ORDER BY rowid")]
        base = (1000 * 2.0 + 500 * 10.0) / 1_000_000
        self.assertAlmostEqual(costs[0], base + 2 * config.WEB_SEARCH_USD)
        self.assertAlmostEqual(costs[1], base + 1 * config.WEB_SEARCH_USD)
        self.assertAlmostEqual(llm.run_spend, sum(costs))

    def test_error_fetch_results_are_skipped_and_pause_limit(self):
        err = t.WebFetchToolResultBlock(type="web_fetch_tool_result", tool_use_id="f", content=t.WebFetchToolResultErrorBlock(
            type="web_fetch_tool_result_error", error_code="url_not_accessible"))
        llm, call = self.go(lambda kw: RMsg("", stop="pause_turn", extra_blocks=[err]))
        r = call()
        self.assertEqual(r.fetched, {})
        self.assertEqual(len(self.ai.calls), 1 + config.RESEARCH_MAX_CONTINUATIONS)

    def test_budget_refusal_happens_before_any_call(self):
        llm, call = self.go(lambda kw: RMsg("x"), run_cap=0.5)
        with self.assertRaises(BudgetExceeded):
            call()
        self.assertEqual(self.ai.calls, [])

    def test_refusal_and_max_tokens(self):
        for stop, exc in (("refusal", LlmRefusal), ("max_tokens", LlmError)):
            llm, call = self.go(lambda kw, s=stop: RMsg("x", stop=s))
            with self.assertRaises(exc):
                call()


class ClaimsExtraTests(unittest.TestCase):
    DOC = {"score": 1.0, "members": [{"appid": 1, "store_url": "https://store.steampowered.com/app/1/", "reviews_total": 400}]}

    def test_numbers_in_text(self):
        self.assertEqual(claims.numbers_in_text("12,345 users, 12.5 and 40% in 2026"), [12345.0, 12.5, 40.0, 2026.0])

    def test_extra_urls_become_citable(self):
        cl = lambda v, u="https://example.com/news/": {"text": "t", "value": v, "url": u}
        extra = {"https://example.com/news": claims.numbers_in_text("sold 12,345 copies, 40% off")}
        self.assertEqual(claims.check([cl(12345), cl(40)], self.DOC, extra), [])
        self.assertTrue(claims.check([cl(999)], self.DOC, extra))
        self.assertTrue(claims.check([cl(12345)], self.DOC))  # without extra the url is unknown
        self.assertTrue(claims.check([cl(12345, "https://store.steampowered.com/app/1/")], self.DOC, extra))
