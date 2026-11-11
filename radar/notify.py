"""Phone push through ntfy (https://ntfy.sh): a morning line from the daily run, a Sunday line from the digest.

WHY: GitHub does not notify you about your own activity, and the digest issue is opened with your own token,
so it never pings you; ntfy is free, needs no account, and its app pushes to iPhone, Mac and Android.
The topic name is the only key (anyone who knows it can post to it or read it), so it lives in the
NTFY_TOPIC secret and never in code. Messages carry counts and public Steam names only, never an idea.
No topic set means nothing is sent; a failed send never fails a run.

All wording lives here (none in the workflows), so every push has the same shape and is tested:
  python -m radar.notify daily --collect-log F --preview-log F --signals-dir D --click URL
  python -m radar.notify daily-failed --click URL
  python -m radar.notify weekly --digest-log F --digest-md F --click URL
  python -m radar.notify monthly|monthly-failed --map-log F --month 2026-11 --click URL
  python -m radar.notify test
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import re
import sys

import requests

log = logging.getLogger(__name__)
SERVER = "https://ntfy.sh"

# Every push has one shape, so it reads on a lock screen without being opened:
#   title  "Steam radar: <which job> <what happened>"   (ASCII: HTTP headers are latin-1)
#   body   what it means in plain words, then "Nothing to do." or what to do, then what a tap opens.
# No internal words (Skeptic, signal, scouted, unvetted): they made the first pushes unreadable (2026-11-11).
# A run started by hand says so, so a test run at 1 am is never taken for the schedule.
PATTERN = "a young game from a small team, growing fast, that looks like a recent small-team hit"


class Push:
    def __init__(self, title: str, body: str, priority: str = "default", tags: str | None = None):
        self.title, self.body, self.priority, self.tags = title, body, priority, tags


def send(message: str, title: str = "Steam radar", priority: str = "default", click: str | None = None,
         tags: str | None = None, topic: str | None = None, post=requests.post) -> bool:
    """Push one message; True when ntfy accepted it. Never raises."""
    topic = topic or os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        log.info("NTFY_TOPIC not set: no push sent")
        return False
    headers = {"Title": title, "Priority": priority}
    if click:
        headers["Click"] = click
    if tags:
        headers["Tags"] = tags
    try:
        resp = post(f"{SERVER}/{topic}", data=message.encode("utf-8"), headers=headers, timeout=20)
        ok = 200 <= resp.status_code < 300
        if not ok:
            log.warning("ntfy answered HTTP %s", resp.status_code)
        return ok
    except requests.RequestException as exc:
        log.warning("ntfy push failed: %s", type(exc).__name__)
        return False


def _read(path: str | None) -> str:
    try:
        with open(path or "", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def manual() -> bool:
    return os.environ.get("GITHUB_EVENT_NAME", "") == "workflow_dispatch"


def _label(push: Push) -> Push:
    if manual():
        push.title += " (manual run)"
        push.body = "Started by hand, not by the schedule. " + push.body
    return push


def daily_message(collect_log: str, preview_log: str, signals_dir: str | None) -> Push:
    """What the daily check saved, and how many games match the pattern today (from a preview on a copy)."""
    m = re.search(r"new_apps=(\d+) meta=\d+ daily=(\d+) steam_calls=\d+ errors=(\d+)", collect_log)
    if not m:
        return _label(Push("Steam radar: Daily check incomplete",
                           "The daily check ran but did not report what it saved, so today's numbers may be "
                           "missing. Nothing else breaks; tomorrow's run tries again. Tap for the log.",
                           "high", "warning"))
    new, daily, errors = (int(x) for x in m.groups())
    out = [f"Nothing to do. Today it saved Steam numbers for {daily:,} games and found {new:,} newly listed ones."]
    if errors:
        out.append(f"{errors} Steam request(s) failed; everything else was saved.")
    num = {k: int(v) for k, v in re.findall(r"^\s*(candidates_after_unusual|signals): (\d+)", preview_log, re.M)}
    if "signals" in num:
        names: list[str] = []
        for f in sorted(glob.glob(os.path.join(signals_dir or "", "signals_*.json"))):
            with open(f, encoding="utf-8") as fh:
                for sig in json.load(fh).get("signals", []):
                    names += [x["name"] for x in sig["members"] if x["role"] == "candidate"][:1]
        if num["signals"]:
            out.append(f"{num['signals']} game(s) match what the radar looks for ({PATTERN})"
                       + (": " + ", ".join(names[:3]) if names else "") + ".")
        else:
            out.append(f"No game matches what the radar looks for today ({PATTERN}); that is normal.")
    out.append("Sunday's digest turns matches into game ideas for you.")
    return _label(Push("Steam radar: Daily check OK", " ".join(out), "low", "white_check_mark"))


def daily_failed() -> Push:
    return _label(Push("Steam radar: Daily check FAILED",
                       "Today's Steam numbers were not saved. Nothing else is lost, and tomorrow's run tries again. "
                       "If this comes twice in a row, ask Claude to look. Tap for the error.", "high", "warning"))


def weekly_message(digest_log: str, digest_md: str = "") -> tuple[Push, bool]:
    """(push, ok) from the digest's summary line; counts only, never an idea's name."""
    m = re.search(r"^digest: week=\S+ kept=(\d+) killed=(\d+) exit=(\d+)", digest_log, re.M)
    spend = re.search(r"API spend this run \$([\d.]+)", digest_md)
    cost = f" API cost ${spend.group(1)}." if spend else ""
    if not m:
        return _label(Push("Steam radar: Weekly ideas FAILED",
                           "The weekly digest crashed before it wrote anything, so there are no game ideas this "
                           "week. Ask Claude to look. Tap for the run log.", "high", "warning")), False
    kept, killed, code = (int(x) for x in m.groups())
    if code:
        push = Push("Steam radar: Weekly ideas incomplete",
                    "Checking the ideas stopped early (an API error or the spend cap), so some were never checked. "
                    f"Before it stopped: {kept} kept, {killed} rejected. Ask Claude to look. "
                    f"Tap to read what finished.{cost}", "high", "warning")
    elif kept:
        push = Push(f"Steam radar: {kept} new game idea(s) this week",
                    f"{kept} game idea(s) passed every check this week and fit your solo Godot hour budget. "
                    f"{killed} other(s) were rejected, each with its reason. Tap to read them.{cost}",
                    "default", "bulb")
    elif killed:
        push = Push("Steam radar: No new game idea this week",
                    f"All {killed} idea(s) found this week were rejected, each with its reason. "
                    f"Nothing to do; tap to read why.{cost}", "default", "zzz")
    else:
        push = Push("Steam radar: No new game idea this week",
                    f"No game matched what the radar looks for this week ({PATTERN}), so there was nothing to turn "
                    f"into an idea. Nothing to do.{cost}", "default", "zzz")
    return _label(push), code == 0


def monthly_message(map_log: str, month: str, ok: bool) -> Push:
    m = re.search(r"^market_map: month=\S+ pairs=(\d+) bullets=\d+ spend=\$([\d.]+)", map_log, re.M)
    if not ok or not m:
        return _label(Push("Steam radar: Monthly market map FAILED",
                           f"The market map for {month} had an error, so its written summary may be missing. "
                           "Nothing else is affected. Ask Claude to look. Tap for the run log.", "high", "warning"))
    pairs, spend = m.groups()
    return _label(Push("Steam radar: Monthly market map ready",
                       f"The {month} map is ready: which kinds of games are getting crowded on Steam and which "
                       f"still work for small teams ({int(pairs):,} tag combinations compared). Nothing to do; "
                       f"tap to read it. API cost ${spend}.", "default", "world_map"))


def test_message() -> Push:
    return Push("Steam radar: Test message",
                "Only a test that notifications reach this phone. Nothing to do. From now on: one message each "
                "morning (daily check), one each Sunday (game ideas), one on the 1st (market map).",
                "default", "bell")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("kind", choices=["daily", "daily-failed", "weekly", "monthly", "monthly-failed", "test"])
    for o in ("--collect-log", "--preview-log", "--signals-dir", "--digest-log", "--digest-md", "--map-log",
              "--month", "--click"):
        ap.add_argument(o)
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    if a.kind == "daily":
        push = daily_message(_read(a.collect_log), _read(a.preview_log), a.signals_dir)
    elif a.kind == "daily-failed":
        push = daily_failed()
    elif a.kind == "weekly":
        push = weekly_message(_read(a.digest_log), _read(a.digest_md))[0]
    elif a.kind in ("monthly", "monthly-failed"):
        push = monthly_message(_read(a.map_log), a.month or "this month", a.kind == "monthly")
    else:
        push = test_message()
    sent = send(push.body, push.title, push.priority, a.click, push.tags)
    print(f"notify: sent={int(sent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
