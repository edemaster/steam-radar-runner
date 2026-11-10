"""Keyless live smoke test: `python -m radar.smoke 730 3000000` prints raw numbers from each endpoint."""
from __future__ import annotations

import sys

from . import steam
from .http import Client


def main(argv: list[str]) -> int:
    """Query ccu, review counts and metadata for the given appids."""
    ids = [int(a) for a in argv] or [730]
    c = Client()
    for a in ids:
        print(a, "ccu", steam.current_players(c, a), "reviews", steam.review_count(c, a))
    for it in steam.get_items(c, ids):
        print({k: it[k] for k in ("appid", "name", "type", "release_date", "coming_soon", "price_cents",
                                  "discount_pct", "is_free", "self_published")} if it and "name" in it else it,
              "tags", len(it.get("tags", [])) if it else 0)
    print("tags known:", len(steam.tag_list(c)), "steam calls:", c.steam_calls)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
