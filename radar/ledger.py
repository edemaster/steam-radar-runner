"""ledger.jsonl: every concept that reached the Skeptic (or failed validation), so none is judged twice.

WHY: the same cluster or the same candidate games re-appear week after week; re-judging them wastes
money and repeats concepts in the digest. One JSON line per concept; the file is committed to git.
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from typing import Any

from . import config


class Ledger:
    def __init__(self, path: str, today: date | None = None) -> None:
        self.path = path
        self.today = today or date.today()
        self.entries: list[dict[str, Any]] = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.entries = [json.loads(line) for line in f if line.strip()]

    def _recent(self) -> list[dict[str, Any]]:
        cutoff = (self.today - timedelta(weeks=config.LEDGER_LOOKBACK_WEEKS)).isoformat()
        return [e for e in self.entries if e.get("week", "") >= cutoff]

    def seen(self, cluster: dict[str, Any]) -> bool:
        """True if the fingerprint is in the ledger, or all its candidates were judged within the lookback."""
        if any(e.get("fingerprint") == cluster["fingerprint"] for e in self.entries):
            return True
        cands = {m["appid"] for m in cluster["members"] if m["role"] == "candidate"}
        if not cands:
            return False
        known = {a for e in self._recent() for a in e.get("candidate_appids", [])}
        return cands <= known

    def recent_titles(self, n: int = 40) -> list[str]:
        return [e["title"] for e in self.entries[-n:] if e.get("title")]

    def add(self, week: str, cluster: dict[str, Any], title: str | None, verdict: str, reason: str,
            snapshot: dict[str, Any] | None = None) -> None:
        entry = {"week": week, "fingerprint": cluster["fingerprint"],
                 "appids": [m["appid"] for m in cluster["members"]],
                 "candidate_appids": [m["appid"] for m in cluster["members"] if m["role"] == "candidate"],
                 "title": title, "verdict": verdict, "reason": reason}
        if snapshot is not None:
            entry["snapshot"] = snapshot
        self.entries.append(entry)

    def save(self) -> None:
        with open(self.path, "w", encoding="utf-8", newline="\n") as f:
            for e in self.entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")


def snapshot_of(cluster: dict[str, Any]) -> dict[str, Any]:
    """Counts of the candidate games when judged, for the follow-up check (radar/followup.py)."""
    return {str(m["appid"]): {"reviews_total": m.get("reviews_total"), "w1": m.get("w1")}
            for m in cluster["members"] if m["role"] == "candidate"}
