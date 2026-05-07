from __future__ import annotations

from datetime import date, timedelta


def longest_streak(dates: list[date]) -> int:
    unique_dates = sorted(set(dates))
    if not unique_dates:
        return 0

    longest = 1
    current = 1
    for previous, current_day in zip(unique_dates, unique_dates[1:]):
        if current_day == previous + timedelta(days=1):
            current += 1
        else:
            longest = max(longest, current)
            current = 1
    return max(longest, current)
