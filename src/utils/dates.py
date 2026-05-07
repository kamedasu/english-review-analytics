from __future__ import annotations

from datetime import date, datetime


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def month_key(value: date) -> str:
    return value.strftime("%Y-%m")


def parse_month_from_title(title: str) -> str | None:
    import re

    match = re.search(r"(20\d{2})[-_/年. ]?(0?[1-9]|1[0-2])", title)
    if not match:
        return None
    return f"{match.group(1)}-{int(match.group(2)):02d}"
