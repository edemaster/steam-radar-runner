"""Phone push through ntfy (https://ntfy.sh): a morning line from the daily run, a Sunday line from the digest.

WHY: GitHub does not notify you about your own activity, and the digest issue is opened with your own token,
so it never pings you; ntfy is free, needs no account, and its app pushes to iPhone, Mac and Android.
The topic name is the only key (anyone who knows it can post to it or read it), so it lives in the
NTFY_TOPIC secret and never in code. Messages carry counts and public Steam names only, never a concept.
No topic set means nothing is sent; a failed send never fails a run.

  python -m radar.notify --title "Radar" [--priority high] [--click URL] [--tags chart] "message"
  python -m radar.notify daily --collect-log F --preview-log F --signals-dir D --click URL
  python -m radar.notify weekly --digest-log F --digest-md F --click URL
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import re

import requests

log = logging.getLogger(__name__)
SERVER = "https://ntfy.sh"


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


def daily_message(collect_log: str, preview_log: str, signals_dir: str | None) -> str:
    """One line: what was collected, and what detection would find if it ran now (from a preview on a copy)."""
    m = re.search(r"new_apps=(\d+) meta=\d+ daily=(\d+) steam_calls=\d+ errors=(\d+)", collect_log)
    out = []
    if m:
        new, daily, errors = (int(x) for x in m.groups())
        out.append(f"Tracked {daily:,} games, {new:,} new on Steam" + (f", {errors} errors." if errors else "."))
    else:
        out.append("Collection finished but printed no summary.")
    num = {k: int(v) for k, v in re.findall(r"^\s*(candidates_after_unusual|signals): (\d+)", preview_log, re.M)}
    if "signals" in num:
        names: list[str] = []
        for f in sorted(glob.glob(os.path.join(signals_dir or "", "signals_*.json"))):
            with open(f, encoding="utf-8") as fh:
                for sig in json.load(fh).get("signals", []):
                    names += [x["name"] for x in sig["members"] if x["role"] == "candidate"][:1]
        out.append(f"{num.get('candidates_after_unusual', 0)} young small-team games growing fast; "
                   f"{num['signals']} possible signal(s) now" + (": " + ", ".join(names[:3]) if names else "") + ".")
    out.append("Next digest: Sunday morning.")
    return " ".join(out)


def weekly_message(digest_log: str, digest_md: str = "") -> tuple[str, bool]:
    """(message, ok) from the digest's summary line; counts only, never a concept's name."""
    m = re.search(r"^digest: week=\S+ kept=(\d+) killed=(\d+) exit=(\d+)", digest_log, re.M)
    spend = re.search(r"API spend this run \$([\d.]+)", digest_md)
    if not m:
        return "The Sunday digest did not finish. Tap for the run log.", False
    kept, killed, code = (int(x) for x in m.groups())
    head = f"{kept} concept(s) kept" if kept else "No concept survived the Skeptic this week"
    msg = f"{head}, {killed} killed" + (f" (API ${spend.group(1)})" if spend else "") + ". Tap to read the digest."
    if code:
        msg += " The API stage stopped early: some signals are unvetted."
    return msg, code == 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    argv = list(argv) if argv is not None else __import__("sys").argv[1:]
    if argv[:1] == ["daily"]:
        ap = argparse.ArgumentParser()
        for o in ("--collect-log", "--preview-log", "--signals-dir", "--click"):
            ap.add_argument(o)
        a = ap.parse_args(argv[1:])
        msg = daily_message(_read(a.collect_log), _read(a.preview_log), a.signals_dir)
        sent = send(msg, "Radar: daily run OK", "low", a.click, "chart_with_upwards_trend")
        print(f"notify: sent={int(sent)}")
        return 0
    if argv[:1] == ["weekly"]:
        ap = argparse.ArgumentParser()
        ap.add_argument("--digest-log")
        ap.add_argument("--digest-md")
        ap.add_argument("--click")
        a = ap.parse_args(argv[1:])
        msg, ok = weekly_message(_read(a.digest_log), _read(a.digest_md))
        sent = send(msg, "Radar: Sunday digest" if ok else "Radar: digest needs a look",
                    "default" if ok else "high", a.click, "mag" if ok else "warning")
        print(f"notify: sent={int(sent)}")
        return 0
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("message")
    ap.add_argument("--title", default="Steam radar")
    ap.add_argument("--priority", default="default", choices=["min", "low", "default", "high", "urgent"])
    ap.add_argument("--click")
    ap.add_argument("--tags")
    a = ap.parse_args(argv)
    sent = send(a.message, a.title, a.priority, a.click, a.tags)
    print(f"notify: sent={int(sent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
