"""CLI: `python -m radar.twitch_snapshot --db radar.db --appids 1,2,3` (called by a later chunk).

WHY: exits 0 with a warning when Twitch is not configured, so the daily workflow never fails on it.
"""
from __future__ import annotations

import argparse
import logging
import sys

from . import config, db
from .http import Client
from .twitch import Twitch, snapshot

log = logging.getLogger("radar.twitch_snapshot")


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="radar.db")
    ap.add_argument("--appids", required=True, help="comma-separated Steam appids")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    creds = config.twitch_credentials()
    if creds is None:
        log.warning("TWITCH_CLIENT_ID/SECRET not set; skipping Twitch snapshot")
        return 0
    appids = [int(x) for x in args.appids.split(",") if x.strip()]
    con = db.connect(args.db)
    written, errors = snapshot(con, Twitch(Client(), *creds), appids)
    log.info("twitch snapshot: %d rows, %d errors", written, errors)
    return 0


if __name__ == "__main__":
    sys.exit(main())
