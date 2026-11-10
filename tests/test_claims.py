import unittest

from radar import claims

S901 = "https://store.steampowered.com/app/901/"
DOC = {
    "score": 1.5, "clone_pressure": {"count": 7, "top": [{"appid": 950, "name": "Clone", "sim": 0.62}]},
    "members": [
        {"appid": 901, "role": "candidate", "store_url": S901,
         "reviews_total": 400, "positive_share": 0.9, "best_match_appid": 900,
         "review_sample": {"n": 100, "share_free": 0.31}, "unusual_pairs": [{"games_with_both": 4}],
         "twitch": {"viewers": 55, "channels": 3, "url": "https://www.twitch.tv/directory/game/g901"}},
        {"appid": 900, "role": "old_hit", "store_url": "https://store.steampowered.com/app/900/",
         "reviews_total": 3000}],
}


def cl(value, url=S901, text="t"):
    return {"text": text, "value": value, "url": url}


class ClaimTests(unittest.TestCase):
    def test_good_claims(self):
        self.assertEqual(claims.check([cl(400), cl(0.31), cl(4), cl(55), cl(1.5), cl(7)], DOC), [])

    def test_url_forms(self):
        self.assertEqual(claims.check([cl(400, "https://store.steampowered.com/app/901?l=en#x")], DOC), [])

    def test_wrong_url(self):
        self.assertTrue(claims.check([cl(3000, S901)], DOC))  # 3000 belongs to app 900
        self.assertTrue(claims.check([cl(400, "https://example.com/901/")], DOC))

    def test_number_not_in_doc(self):
        self.assertTrue(claims.check([cl(410)], DOC))
        self.assertTrue(claims.check([cl(901)], DOC))  # an appid is not a citable number

    def test_percent_form(self):
        self.assertEqual(claims.check([cl(31), cl(90)], DOC), [])
        self.assertTrue(claims.check([cl(32)], DOC))

    def test_clone_url(self):
        u = "https://store.steampowered.com/app/950/"
        self.assertEqual(claims.check([cl(0.62, u), cl(62, u), cl(7, u)], DOC), [])
        self.assertTrue(claims.check([cl(400, u)], DOC))

    def test_twitch_url_only_twitch_numbers(self):
        u = "https://www.twitch.tv/directory/game/g901"
        self.assertEqual(claims.check([cl(55, u)], DOC), [])
        self.assertTrue(claims.check([cl(400, u)], DOC))

    def test_zero_claims_and_malformed(self):
        self.assertTrue(claims.check([], DOC))
        self.assertTrue(claims.check(None, DOC))
        self.assertTrue(claims.check([{"text": "x", "value": "400", "url": S901}], DOC))

    def test_any_invalid_claim_fails_the_list(self):
        self.assertEqual(len(claims.check([cl(400), cl(12345)], DOC)), 1)
