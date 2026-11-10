"""One cumulative review series per app, merging backfilled points and daily rows.

WHY: backfill (review_points) and the daily collector (daily) both record cumulative totals; detection
must not care which wrote a date. Daily wins on a shared date because it is the fresher measurement.
Counts only.
"""
from __future__ import annotations

import bisect
import sqlite3
from datetime import date, timedelta

Point = tuple[date, int, int]  # (day, total, positive)


def cumulative(con: sqlite3.Connection, appid: int) -> list[Point]:
    """All (date, total, positive) points for an app, date-sorted, daily overriding review_points."""
    merged: dict[date, tuple[int, int]] = {}
    for r in con.execute("SELECT upto, reviews_total, reviews_positive FROM review_points WHERE appid=?",
                         (appid,)):
        if r[1] is not None:
            merged[date.fromisoformat(r[0])] = (int(r[1]), int(r[2] or 0))
    for r in con.execute("SELECT day, reviews_total, reviews_positive FROM daily "
                         "WHERE appid=? AND reviews_total IS NOT NULL", (appid,)):
        merged[date.fromisoformat(r[0])] = (int(r[1]), int(r[2] or 0))
    return [(d, t, p) for d, (t, p) in sorted(merged.items())]


def value_at(series: list[Point], day: date) -> Point | None:
    """The last point on or before `day`, or None when the series starts later."""
    i = bisect.bisect_right([p[0] for p in series], day)
    return series[i - 1] if i else None


def reviews_between(series: list[Point], d0: date, d1: date) -> int | None:
    """Reviews gained after d0 up to d1, using the last point <= each date.

    A series that starts after d0 counts as 0 at d0 (nothing existed before the first point: backfill's
    first point is release+7, daily starts at tracking). None when no point <= d1 exists.
    """
    end = value_at(series, d1)
    if end is None:
        return None
    start = value_at(series, d0)
    return end[1] - (start[1] if start else 0)


def window(series: list[Point], day: date, days: int) -> int | None:
    """Reviews gained in the `days` ending at `day`."""
    return reviews_between(series, day - timedelta(days=days), day)
