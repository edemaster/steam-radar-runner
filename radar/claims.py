"""No link, no card: every claim a model makes must be a number from the signal document, with its source link.

WHY: a model can invent a plausible number or URL. A claim is {"text", "value", "url"}; the value must
equal (within a small tolerance) a number the document holds for that exact URL, so each figure in the
digest can be clicked and checked. A card with no claims, or any bad claim, is rejected by the caller.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

SKIP_KEYS = {"appid", "best_match_appid"}


def norm_url(url: str) -> str:
    """Compare URLs without query, fragment or a trailing slash."""
    p = urlsplit(str(url).strip())
    return f"{p.scheme}://{p.netloc}{p.path}".rstrip("/")


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def numeric_leaves(obj: Any) -> list[float]:
    """Every number inside obj (recursively), except the id keys in SKIP_KEYS."""
    out: list[float] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k not in SKIP_KEYS:
                out.extend(numeric_leaves(v))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out.extend(numeric_leaves(v))
    elif _is_num(obj):
        out.append(float(obj))
    return out


_NUM_RE = re.compile(r"(?<![\w.])\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.])\d+(?:\.\d+)?")


def numbers_in_text(text: str) -> list[float]:
    """Every number in page text: 12,345 / 12.5 / 40% (the percent sign is not part of the number)."""
    return [float(m.group(0).replace(",", "")) for m in _NUM_RE.finditer(text or "")]


def allowed(doc: dict[str, Any], extra: dict[str, list[float]] | None = None) -> dict[str, list[float]]:
    """Map normalised url -> the numbers a claim may cite there; `extra` (fetched pages) adds more urls."""
    cluster = [float(doc["score"])] if _is_num(doc.get("score")) else []
    cp = doc.get("clone_pressure") or {}
    if _is_num(cp.get("count")):
        cluster.append(float(cp["count"]))
    out: dict[str, list[float]] = {}

    def add(url: str, nums: list[float]) -> None:
        out.setdefault(norm_url(url), []).extend(nums)

    for m in doc.get("members", []):
        add(m["store_url"], numeric_leaves(m) + cluster)
        tw = m.get("twitch")
        if tw and tw.get("url"):
            add(tw["url"], numeric_leaves({k: v for k, v in tw.items() if k != "url"}))
    for c in cp.get("top", []):
        add(f"https://store.steampowered.com/app/{c['appid']}/", ([float(c["sim"])] if _is_num(c.get("sim")) else []) + cluster)
    for url, nums in (extra or {}).items():
        add(url, [float(n) for n in nums])
    for url, nums in out.items():  # a share in [0,1] may be quoted as a percent
        out[url] = nums + [n * 100 for n in nums if 0 <= n <= 1]
    return out


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(0.01, 0.005 * abs(b))


def check(claims: Any, doc: dict[str, Any], extra: dict[str, list[float]] | None = None) -> list[str]:
    """Error strings for a claim list; empty means every claim is backed by the document."""
    if not isinstance(claims, list) or not claims:
        return ["no claims: a card needs at least one claim with a value and a source url"]
    ok = allowed(doc, extra)
    errors = []
    for i, c in enumerate(claims):
        label = f"claim {i + 1}"
        if not isinstance(c, dict) or not _is_num(c.get("value")) or not isinstance(c.get("url"), str) \
                or not isinstance(c.get("text"), str):
            errors.append(f"{label}: must be {{text, value (number), url}}")
            continue
        nums = ok.get(norm_url(c["url"]))
        if nums is None:
            errors.append(f"{label}: url {c['url']} is not a link in the document")
        elif not any(_close(float(c["value"]), n) for n in nums):
            errors.append(f"{label}: value {c['value']} does not appear in the document for {c['url']}")
    return errors
