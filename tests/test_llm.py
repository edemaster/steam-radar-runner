import os
import tempfile
import unittest
from datetime import datetime, timezone

import anthropic

try:  # the SDK's own HTTP library (httpx2 in anthropic 2.x)
    import httpx2 as httpx
except ImportError:
    import httpx

from radar import config, db
from radar.llm import BudgetExceeded, Llm, LlmError, LlmRefusal, OutOfCredit

from .fakes import FakeAnthropic, Msg

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["a"], "properties": {"a": {"type": "integer"}}}
M = config.SCOUT_MODEL


def status_error(code, message, etype="invalid_request_error"):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(code, request=req)
    return anthropic.APIStatusError(message, response=resp, body={"error": {"type": etype, "message": message}})


def spend_row(con, ts, usd):
    con.execute("INSERT INTO spend(ts, model, input_tokens, output_tokens, usd, purpose) VALUES(?,?,?,?,?,?)",
                (ts, "m", 0, 0, usd, "x"))


class LlmTests(unittest.TestCase):
    def setUp(self):
        self.con = db.connect(os.path.join(tempfile.mkdtemp(), "t.db"))

    def llm(self, fn, **kw):
        self.client = FakeAnthropic({M: fn})
        return Llm(self.client, self.con, **kw)

    def call(self, llm):
        return llm.call_json(M, "medium", 4000, "sys", "user", SCHEMA, "t")

    def test_ok_writes_spend_with_real_usage(self):
        llm = self.llm(lambda kw: Msg({"a": 1}, i=2_000_000, o=1_000_000), run_cap=100, monthly_cap=100)
        self.assertEqual(self.call(llm), {"a": 1})
        row = self.con.execute("SELECT * FROM spend").fetchone()
        self.assertEqual((row["model"], row["input_tokens"], row["output_tokens"], row["purpose"]),
                         (M, 2_000_000, 1_000_000, "t"))
        pin, pout = config.PRICES[M]
        self.assertAlmostEqual(row["usd"], 2 * pin + pout)
        self.assertAlmostEqual(llm.run_spend, 2 * pin + pout)
        kw = self.client.calls[0]
        self.assertEqual(kw["output_config"]["format"]["type"], "json_schema")
        for banned in ("temperature", "top_p", "top_k", "thinking"):
            self.assertNotIn(banned, kw)

    def test_monthly_cap_blocks_before_calling(self):
        spend_row(self.con, datetime.now(timezone.utc).isoformat(timespec="seconds"), config.LLM_MONTHLY_CAP_USD - 0.001)
        llm = self.llm(lambda kw: Msg({"a": 1}))
        with self.assertRaises(BudgetExceeded):
            self.call(llm)
        self.assertEqual(self.client.calls, [])

    def test_last_months_spend_does_not_count(self):
        spend_row(self.con, "2020-01-05T00:00:00", 99)
        self.assertEqual(self.call(self.llm(lambda kw: Msg({"a": 1}))), {"a": 1})

    def test_run_cap_blocks_before_calling(self):
        llm = self.llm(lambda kw: Msg({"a": 1}), run_cap=0.0001)
        with self.assertRaises(BudgetExceeded):
            self.call(llm)
        self.assertEqual(self.client.calls, [])

    def test_billing_errors_are_out_of_credit(self):
        for err in (status_error(402, "payment required"),
                    status_error(400, "x", etype="billing_error"),
                    status_error(400, "Your credit balance is too low"),
                    status_error(429, "You have reached your usage limit")):
            with self.assertRaises(OutOfCredit):
                self.call(self.llm(lambda kw, e=err: e))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM spend").fetchone()[0], 0)

    def test_other_api_error_is_llm_error_and_redacted(self):
        with self.assertRaises(LlmError) as cm:
            self.call(self.llm(lambda kw: status_error(500, "boom key=SECRET123")))
        self.assertNotIn("SECRET123", str(cm.exception))
        self.assertNotIsInstance(cm.exception, OutOfCredit)

    def test_refusal_and_max_tokens(self):
        with self.assertRaises(LlmRefusal):
            self.call(self.llm(lambda kw: Msg("", stop="refusal")))
        with self.assertRaises(LlmError) as cm:
            self.call(self.llm(lambda kw: Msg('{"a"', stop="max_tokens")))
        self.assertNotIsInstance(cm.exception, LlmRefusal)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM spend").fetchone()[0], 2)  # tokens were still paid for
