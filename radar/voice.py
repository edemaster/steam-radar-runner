"""Player voice: what a candidate's players love, complain about and wish for, from its recent reviews.

WHY: a twist built on a real complaint is worth more than one built on tags. The model only summarises;
the counts it returns are overwritten with real ones, and any quote that is not verbatim in a given review
is dropped, so an invented quote can never reach the digest. Reviews are read without author data (steam.review_texts).
"""
from __future__ import annotations

import json
import re
from typing import Any

from . import config, steam
from .http import Client
from .llm import Llm

ITEM = {"type": "object", "additionalProperties": False, "required": ["point", "mentions", "quote"],
        "properties": {"point": {"type": "string"}, "mentions": {"type": "integer"}, "quote": {"type": "string"}}}
ITEMS = {"type": "array", "items": ITEM}
VOICE_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["n_read", "n_positive", "loves", "complaints", "wishes", "audience"],
    "properties": {"n_read": {"type": "integer"}, "n_positive": {"type": "integer"}, "loves": ITEMS,
                   "complaints": ITEMS, "wishes": ITEMS, "audience": {"type": "string"}}}

VOICE_SYSTEM = """You summarise recent Steam reviews of one game for a game designer.
You get a JSON list of reviews: text, up (thumbs up) and hours (playtime of the reviewer).
Say what players love, what they complain about and what they wish existed, and who seems to play it (use hours).
Rules:
- Count mentions from the given reviews only; never use outside knowledge of the game.
- Each quote is copied verbatim from ONE review, at most 25 words.
- At most 5 items per list. Leave a list empty when the reviews do not say."""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def player_voice(llm: Llm, client: Client, appid: int) -> dict[str, Any] | None:
    """Summary dict for the digest, or None when there are too few reviews. Steam errors propagate."""
    reviews = steam.review_texts(client, appid)
    if len(reviews) < config.VOICE_MIN_REVIEWS:
        return None
    data = llm.call_json(config.VOICE_MODEL, config.VOICE_EFFORT, config.VOICE_MAX_TOKENS, VOICE_SYSTEM,
                         json.dumps(reviews, ensure_ascii=False), VOICE_SCHEMA, "voice")
    haystack = [_norm(r["text"]) for r in reviews]
    data["n_read"] = len(reviews)
    data["n_positive"] = sum(1 for r in reviews if r["up"])
    for key in ("loves", "complaints", "wishes"):
        data[key] = [it for it in (data.get(key) or [])
                     if _norm(str(it.get("quote", ""))) and any(_norm(it["quote"]) in h for h in haystack)]
    return data
