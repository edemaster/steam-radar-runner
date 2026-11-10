import json
import unittest

from radar import steam
from radar.http import HttpError

from .fakes import FakeClient, fixture


class SteamTests(unittest.TestCase):
    def test_parse_items(self):
        items = fixture("items.json")["response"]["store_items"]
        a, b, c, d = (steam.parse_item(i) for i in items)
        self.assertEqual((a["type"], a["price_cents"], a["discount_pct"], a["self_published"]), ("game", 999, 50, 1))
        self.assertEqual(a["release_date"], "2026-10-26")
        self.assertEqual(a["tags"], [(19, 500), (492, 300)])
        self.assertEqual((b["coming_soon"], b["release_date"], b["is_free"], b["price_cents"], b["self_published"]),
                         (1, None, 1, 0, 0))
        self.assertEqual(c["type"], "type_1")
        self.assertTrue(d["unavailable"])

    def test_app_list_pages(self):
        pages = [{"response": {"apps": [{"appid": 1, "name": "a"}, {"appid": 2, "name": "b"}],
                               "have_more_results": True, "last_appid": 2}},
                 {"response": {"apps": [{"appid": 3, "name": "c"}]}}]
        seen = []

        def h(p):
            seen.append(p["last_appid"])
            return pages.pop(0)
        out = list(steam.app_list(FakeClient({"GetAppList": h}), "K"))
        self.assertEqual([a["appid"] for a in out], [1, 2, 3])
        self.assertEqual(seen, [0, 2])

    def test_review_count_params_and_parse(self):
        got = {}

        def h(p):
            got.update(p)
            return {"response": {"query_summary": {"total_reviews": 12, "total_positive": 9}}}
        c = FakeClient({"GetAppReviews": h})
        self.assertEqual(steam.review_count(c, 5, 100, 200), (12, 9))
        self.assertEqual((got["day_range"], got["num_per_page"], got["date_range_start"]), (0, 1, 100))
        got.clear()
        steam.review_count(c, 5)
        self.assertNotIn("date_range_start", got)

    def test_review_count_empty(self):
        c = FakeClient({"GetAppReviews": lambda p: {"response": {"query_summary": {"num_reviews": 0},
                                                                  "total_matching": 0}}})
        self.assertEqual(steam.review_count(c, 5), (0, 0))

    def test_ccu(self):
        c = FakeClient({"GetNumberOfCurrentPlayers": lambda p: {"response": {"player_count": 42, "result": 1}}})
        self.assertEqual(steam.current_players(c, 1), 42)

    def test_ccu_404_is_none(self):
        def h(p):
            raise HttpError("x", 404)
        self.assertIsNone(steam.current_players(FakeClient({"GetNumberOfCurrentPlayers": h}), 1))

    def test_get_items_splits_failing_batch(self):
        def h(p):
            ids = [i["appid"] for i in json.loads(p["input_json"])["ids"]]
            if 100 in ids and len(ids) > 1:
                raise HttpError("boom")
            if ids == [100]:
                raise HttpError("boom")
            return {"response": {"store_items": [{"id": i, "success": 1, "name": str(i), "type": 0} for i in ids]}}
        out = steam.get_items(FakeClient({"GetItems": h}), [100, 101, 102, 103])
        self.assertIsNone(out[0])
        self.assertEqual([o["appid"] for o in out[1:]], [101, 102, 103])


if __name__ == "__main__":
    unittest.main()
