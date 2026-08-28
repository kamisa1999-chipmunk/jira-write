"""Working-day helpers (Mon–Fri). Holidays are not excluded yet."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Optional, Union

WORKDAY_START = time(10, 0)
WORKDAY_END = time(18, 0)
HOURS_PER_WORKDAY = 8.0


def business_hours(
    start: datetime,
    end: datetime,
    *,
    day_start: time = WORKDAY_START,
    day_end: time = WORKDAY_END,
) -> float:
    """Sum hours inside Mon–Fri work windows between start and end."""
    if end <= start:
        return 0.0
    if start.tzinfo is not None and end.tzinfo is None:
        end = end.replace(tzinfo=start.tzinfo)
    elif end.tzinfo is not None and start.tzinfo is None:
        start = start.replace(tzinfo=end.tzinfo)

    total = 0.0
    day = start.date()
    last = end.date()
    while day <= last:
        if day.weekday() < 5:
            a = max(start, datetime.combine(day, day_start, tzinfo=start.tzinfo))
            b = min(end, datetime.combine(day, day_end, tzinfo=start.tzinfo))
            if b > a:
                total += (b - a).total_seconds() / 3600.0
        day += timedelta(days=1)
    return total


def business_days(
    start: datetime,
    end: datetime,
    *,
    hours_per_workday: float = HOURS_PER_WORKDAY,
    day_start: time = WORKDAY_START,
    day_end: time = WORKDAY_END,
) -> float:
    """Working days as business_hours / hours_per_workday."""
    if hours_per_workday <= 0:
        return 0.0
    return business_hours(start, end, day_start=day_start, day_end=day_end) / hours_per_workday


def remaining_working_days_until(
    end: Union[datetime, date, str, None],
    *,
    now: Optional[datetime] = None,
    hours_per_workday: float = HOURS_PER_WORKDAY,
) -> Optional[float]:
    """Working days from now until end (sprint endDate)."""
    if end is None:
        return None
    if isinstance(end, str):
        from models.issue import parse_jira_datetime

        end_dt = parse_jira_datetime(end)
    elif isinstance(end, date) and not isinstance(end, datetime):
        end_dt = datetime.combine(end, time(23, 59, 59))
    else:
        end_dt = end

    current = now or datetime.now().astimezone()
    if end_dt.tzinfo is None and current.tzinfo is not None:
        end_dt = end_dt.replace(tzinfo=current.tzinfo)
    if current >= end_dt:
        return 0.0
    return round(business_days(current, end_dt, hours_per_workday=hours_per_workday), 2)
