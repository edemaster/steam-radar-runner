import json
import os
import tempfile
import unittest
from unittest import mock

from radar import config, db, digest
from radar.llm import Llm

from .fakes import FakeAnthropic, Msg
from .test_llm import status_error

WEEK = "2026-11-10"
BRIEF = "## Time\n\n* Hours per week available: 1 a day\n* Max weeks for a prototype (2-4): 2\n"


def member(appid, role="candidate", name=None):
    m = {"appid": appid, "role": role, "name": name or f"Game {appid}",
         "store_url": f"https://store.steampowered.com/app/{appid}/", "release_date": "2026-10-01",
         "price_cents": 999, "is_free": 0, "dev_app_count": 1, "pub_app_count": 1,
         "reviews_total": 400 + appid, "reviews_positive": 360, "positive_share": 0.9}
    if role == "candidate":
        m.update({"w1": 60, "w0": 50})
    return m


def cluster(fp, score, base):
    return {"week": WEEK, "fingerprint": fp, "score": score, "shared_tags": ["Cozy", "Robots"],
            "members": [member(base), member(base + 1, "old_hit")],
            "clone_pressure": {"count": 2, "top": []}}


A, B, C = cluster("fpA", 2.0, 100), cluster("fpB", 1.0, 200), cluster("fpC", 0.5, 300)


def good_claims(base):
    return [{"text": "reviews", "value": 400 + base, "url": f"https://store.steampowered.com/app/{base}/"},
            {"text": "growth", "value": 60, "url": f"https://store.steampowered.com/app/{base}/"},
            {"text": "score", "value": 0.9, "url": f"https://store.steampowered.com/app/{base}/"}]


def card(title, claims):
    return {"title": title, "pitch": "P1. P2.", "core_mechanic": "mech", "twist": "twist", "inspired_by_appids": [],
            "claims": claims, "solo_scope": {"art": "a", "content": "c", "systems": "s"},
            "prototype_plan": ["step one", "step two"]}


def verdict(v, conf=3, reason="solo_scope"):
    return {"verdict": v, "kill_reasons": [] if v == "keep" else [{"check": reason, "text": f"too big {reason}"}],
            "claims": [], "scope_hours_estimate": 12, "brief_fit": 4,
            "brief_fit_notes": "fits", "keep_case": "survives all checks", "risks": ["risk one"], "confidence": conf}


def base_of(content):
    for fp, base in (("fpA", 100), ("fpB", 200), ("fpC", 300)):
        if f'"{fp}"' in content:
            return fp, base
    raise AssertionError("no fingerprint in prompt")


class DigestTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.dir = d
        self.db = os.path.join(d, "t.db")
        self.sig = os.path.join(d, "signals.json")
        self.brief = os.path.join(d, "BRIEF.md")
        self.ledger = os.path.join(d, "ledger.jsonl")
        self.out = os.path.join(d, "digests")
        with open(self.brief, "w", encoding="utf-8") as f:
            f.write(BRIEF)
        self.con = db.connect(self.db)
        self.con.execute("INSERT INTO daily VALUES(1,'2026-11-09',10,5,5)")
        self.con.commit()

    def signals(self, clusters):
        with open(self.sig, "w", encoding="utf-8") as f:
            json.dump({"week": WEEK, "signals": clusters}, f)

    def run_digest(self, scout_fn, skeptic_fn, **kw):
        self.client = FakeAnthropic({config.SCOUT_MODEL: scout_fn, config.SKEPTIC_MODEL: skeptic_fn})
        llm = Llm(self.client, db.connect(self.db))
        code = digest.run(self.db, self.sig, self.brief, self.ledger, self.out, llm=llm, **kw)
        with open(os.path.join(self.out, f"{WEEK}.md"), encoding="utf-8") as f:
            return code, f.read()

    def ledger_rows(self):
        with open(self.ledger, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]

    def scout_ok(self, kw):
        fp, base = base_of(kw["messages"][0]["content"])
        return Msg(card(f"Concept {fp}", good_claims(base)))

    def test_zero_survivors(self):
        self.signals([A, B])
        code, text = self.run_digest(self.scout_ok, lambda kw: Msg(verdict("kill")))
        self.assertEqual(code, 0)
        self.assertIn("No concept survived this week", text)
        self.assertIn("Killed this week", text)
        self.assertIn("solo_scope", text)
        self.assertEqual([r["verdict"] for r in self.ledger_rows()], ["kill", "kill"])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM spend").fetchone()[0], 4)

    def test_two_survivors_ranked_by_confidence_then_score(self):
        self.signals([A, B, C])

        def sk(kw):
            fp, _ = base_of(kw["messages"][0]["content"])
            return Msg({"fpA": verdict("keep", 3), "fpB": verdict("keep", 5), "fpC": verdict("kill")}[fp])
        code, text = self.run_digest(self.scout_ok, sk)
        self.assertEqual(code, 0)
        self.assertLess(text.index("Concept fpB"), text.index("Concept fpA"))
        self.assertIn("## 1. Concept fpB", text)
        self.assertIn("([source](https://store.steampowered.com/app/200/))", text)
        self.assertIn("time: 1 a day; max weeks: 2", text)
        self.assertIn("Confidence: 5/5", text)
        self.assertEqual(sorted(r["verdict"] for r in self.ledger_rows()), ["keep", "keep", "kill"])
        self.assertIn("2 kept", text)

    def test_invalid_card_is_retried_then_dropped(self):
        self.signals([A, B])
        bad = [dict(c, value=99999) for c in good_claims(100)]

        def scout_fn(kw):
            content = kw["messages"][0]["content"]
            fp, base = base_of(content)
            if fp == "fpA":  # never valid
                return Msg(card("Bad", bad))
            return Msg(card("Concept fpB", good_claims(base)))
        code, text = self.run_digest(scout_fn, lambda kw: Msg(verdict("keep", 4)))
        scout_calls = [c for c in self.client.calls if c["model"] == config.SCOUT_MODEL]
        a_calls = [c for c in scout_calls if '"fpA"' in c["messages"][0]["content"]]
        self.assertEqual(len(a_calls), 2)
        self.assertIn("rejected", a_calls[1]["messages"][0]["content"])
        self.assertIn("does not appear in the document", a_calls[1]["messages"][0]["content"])
        rows = {r["fingerprint"]: r for r in self.ledger_rows()}
        self.assertEqual(rows["fpA"]["verdict"], "invalid")
        self.assertEqual(rows["fpB"]["verdict"], "keep")
        self.assertIn("Concept fpB", text)
        self.assertNotIn("## 1. Bad", text)
        self.assertEqual(len([c for c in self.client.calls if c["model"] == config.SKEPTIC_MODEL]), 1)

    def test_retry_that_succeeds_is_used(self):
        self.signals([A])
        n = []

        def scout_fn(kw):
            n.append(1)
            return Msg(card("T", [dict(good_claims(100)[0], value=77777)] if len(n) == 1 else good_claims(100)))
        code, text = self.run_digest(scout_fn, lambda kw: Msg(verdict("keep")))
        self.assertEqual(len(n), 2)
        self.assertIn("## 1. T", text)

    def test_removed_skeptic_claim_is_noted_not_fatal(self):
        self.signals([A])
        sk = verdict("keep")
        sk["claims"] = [{"text": "made up", "value": 12345, "url": "https://store.steampowered.com/app/100/"}]
        code, text = self.run_digest(self.scout_ok, lambda kw: Msg(sk))
        self.assertEqual(code, 0)
        self.assertIn("Removed a Skeptic claim", text)
        self.assertIn("## 1. Concept fpA", text)

    def test_out_of_credit_banner_and_exit_1(self):
        self.signals([A, B])

        def sk(kw):
            return status_error(402, "Your credit balance is too low")
        code, text = self.run_digest(self.scout_ok, sk)
        self.assertEqual(code, 1)
        self.assertTrue(text.split("\n")[2].startswith("> **Anthropic API: out of credit"))
        self.assertIn("console.anthropic.com/settings/billing", text)
        self.assertIn("nothing was vetted", text)
        self.assertIn("Unvetted signals (LLM stage stopped)", text)
        self.assertIn("Game 100", text)
        self.assertFalse(os.path.exists(self.ledger) and any(r["verdict"] in ("keep", "kill") for r in self.ledger_rows()))

    def test_budget_exceeded_banner(self):
        self.signals([A])
        self.client = FakeAnthropic({})
        llm = Llm(self.client, db.connect(self.db), run_cap=0.00001)
        code = digest.run(self.db, self.sig, self.brief, self.ledger, self.out, llm=llm)
        with open(os.path.join(self.out, f"{WEEK}.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(code, 1)
        self.assertIn("Spend cap reached", text)
        self.assertEqual(self.client.calls, [])

    def test_seen_clusters_are_skipped_before_the_scout(self):
        self.signals([A, B])
        with open(self.ledger, "w", encoding="utf-8") as f:
            f.write(json.dumps({"week": "2026-11-03", "fingerprint": "fpA", "appids": [100, 101],
                                "candidate_appids": [100], "title": "Old", "verdict": "kill", "reason": "x"}) + "\n")
        code, text = self.run_digest(self.scout_ok, lambda kw: Msg(verdict("kill")))
        self.assertTrue(all('"fpA"' not in c["messages"][0]["content"] or "already_judged" in c["messages"][0]["content"]
                            for c in self.client.calls))
        self.assertEqual(len([c for c in self.client.calls if c["model"] == config.SCOUT_MODEL]), 1)
        self.assertIn("1 cluster(s) skipped as already seen", text)

    def test_no_llm_makes_no_calls_and_writes_no_ledger(self):
        self.signals([A, B])
        with mock.patch("anthropic.Anthropic", side_effect=AssertionError("API touched")):
            code = digest.run(self.db, self.sig, self.brief, self.ledger, self.out, no_llm=True)
        with open(os.path.join(self.out, f"{WEEK}.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(code, 0)
        self.assertIn("Unvetted signals (no LLM run)", text)
        self.assertIn("[Game 100](https://store.steampowered.com/app/100/)", text)
        self.assertFalse(os.path.exists(self.ledger))
        self.assertNotIn("No concept survived", text)

    def test_twitch_enrichment_and_appids(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.con.execute("INSERT INTO twitch_live VALUES(100,'g',?,3,55)", (now,))
        self.con.execute("INSERT INTO twitch_live VALUES(200,'g','2020-01-01T00:00:00+00:00',3,55)")
        self.con.commit()
        sigs = [json.loads(json.dumps(A)), json.loads(json.dumps(B))]
        digest.enrich_twitch(self.con, sigs)
        self.assertEqual(sigs[0]["members"][0]["twitch"]["viewers"], 55)
        self.assertEqual(sigs[0]["members"][0]["twitch"]["url"], "https://www.twitch.tv/directory/game/Game%20100")
        self.assertNotIn("twitch", sigs[1]["members"][0])
        self.signals([A, B])
        self.assertEqual(digest.member_appids(digest.load_signals(self.sig)[1]), [100, 101, 200, 201])
