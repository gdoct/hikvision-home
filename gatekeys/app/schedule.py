"""
When is a code allowed to open the gate?

A code carries a date range (inclusive, both ends) and a set of weekly
windows: weekday plus a start and end time on a 15-minute grid. "Mondays and
Tuesdays, 07:15-13:00 and 15:45-20:30, from 1 December 2026 to 1 March 2032"
is four windows (two days x two windows) plus the date range.

Everything is evaluated in LOCAL time (TZ, default UTC — set it to your own
in .env, e.g. Europe/Amsterdam), because
that is what the person holding the code reads off their own clock. DST is
handled by zoneinfo: on the two switch days a window keeps its wall-clock
times, which is the intent — "mornings" does not shift by an hour in October.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("TZ") or "UTC")
STEP_MINUTES = 15
DAY_MINUTES = 24 * 60
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def now_local() -> datetime:
    return datetime.now(TZ)


def parse_hhmm(text: str) -> int:
    """'07:15' -> 435. Rejects anything off the 15-minute grid."""
    try:
        hours, _, minutes = text.strip().partition(":")
        total = int(hours) * 60 + int(minutes)
    except ValueError:
        raise ValueError(f"not a time: {text!r}")
    if not 0 <= total <= DAY_MINUTES:
        raise ValueError(f"time out of range: {text!r}")
    if total % STEP_MINUTES:
        raise ValueError(f"times must be on a {STEP_MINUTES} minute grid: {text!r}")
    return total


def format_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_date(text: str) -> date:
    return date.fromisoformat(text.strip())


def clean_windows(raw: list[dict]) -> list[tuple[int, int, int]]:
    """Validate GUI input into (weekday, start_min, end_min) triples.

    Each item is {"weekdays": [0, 1], "start": "07:15", "end": "13:00"} — one
    row in the GUI can cover several days, which is how people think about it
    ("Mon and Tue, mornings").
    """
    out: list[tuple[int, int, int]] = []
    for item in raw:
        start = parse_hhmm(str(item.get("start", "")))
        end = parse_hhmm(str(item.get("end", "")))
        if end <= start:
            raise ValueError(
                f"window ends before it starts ({format_hhmm(start)}-{format_hhmm(end)}); "
                "for a window over midnight, add one on each day"
            )
        days = item.get("weekdays") or []
        if not days:
            raise ValueError("a window needs at least one weekday")
        for day in days:
            day = int(day)
            if not 0 <= day <= 6:
                raise ValueError(f"no such weekday: {day}")
            out.append((day, start, end))
    if not out:
        raise ValueError("a code needs at least one window, or it can never be used")
    # Same day and overlapping is not wrong, just confusing to read back.
    return sorted(set(out))


def windows_allow(windows: list[dict], when: datetime) -> bool:
    minutes = when.hour * 60 + when.minute
    weekday = when.weekday()
    return any(
        w["weekday"] == weekday and w["start_min"] <= minutes < w["end_min"]
        for w in windows
    )


def describe(windows: list[dict]) -> list[str]:
    """Human summary, grouping days that share the same times:
    ['Mon, Tue 07:15-13:00', 'Mon, Tue 15:45-20:30']."""
    by_time: dict[tuple[int, int], list[int]] = {}
    for w in windows:
        by_time.setdefault((w["start_min"], w["end_min"]), []).append(w["weekday"])
    lines = []
    for (start, end), days in sorted(by_time.items()):
        names = ", ".join(WEEKDAYS[d] for d in sorted(set(days)))
        lines.append(f"{names} {format_hhmm(start)}-{format_hhmm(end)}")
    return lines


def next_opening(windows: list[dict], valid_from: date, valid_to: date,
                 when: datetime, horizon_days: int = 400) -> datetime | None:
    """First moment from `when` at which these windows allow entry, or None
    within the horizon. Shown in the GUI so 'why can't they get in' has an
    answer that is not a puzzle."""
    minutes = when.hour * 60 + when.minute
    for offset in range(horizon_days):
        day = (when + _days(offset)).date()
        if day < valid_from:
            continue
        if day > valid_to:
            return None
        starts = sorted(
            w["start_min"] for w in windows
            if w["weekday"] == day.weekday() and (offset > 0 or w["end_min"] > minutes)
        )
        if starts:
            start = starts[0]
            if offset == 0:
                # Already inside a window: the answer is "now".
                if windows_allow(windows, when):
                    return when
                start = min(s for s in starts if s > minutes) if any(s > minutes for s in starts) else None
                if start is None:
                    continue
            return datetime.combine(day, datetime.min.time(), TZ).replace(
                hour=start // 60, minute=start % 60
            )
    return None


def _days(n: int):
    from datetime import timedelta
    return timedelta(days=n)
