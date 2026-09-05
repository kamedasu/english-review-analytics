from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from calendar import monthrange


DEFAULT_RESULT_COUNT = 5
MAX_REQUESTED_RESULTS = 30


@dataclass(frozen=True)
class RagQueryIntent:
    kind: str
    requested_count: int
    recent: bool
    has_explicit_count: bool
    start_date: date | None = None
    end_date: date | None = None
    requires_reference_date: bool = False


def parse_rag_query(query: str, reference_date: date | None = None) -> RagQueryIntent:
    normalized = query.casefold()
    date_range, requires_reference_date = _date_range(normalized, reference_date)
    return RagQueryIntent(
        kind=_intent_kind(normalized),
        requested_count=_requested_count(normalized),
        recent=any(marker in normalized for marker in ("最近", "直近", "recent", "recently", "latest")),
        has_explicit_count=_has_requested_count(normalized),
        start_date=date_range[0] if date_range else None,
        end_date=date_range[1] if date_range else None,
        requires_reference_date=requires_reference_date,
    )


def _intent_kind(query: str) -> str:
    if _contains_any(query, ("自然なフレーズ", "自然な言い回し", "直された", "より自然", "natural expression", "natural expressions", "corrected", "correction")):
        return "natural_expression"
    if _contains_any(query, ("覚えたほうが", "使ったほうが", "ネイティブ", "おすすめ", "使えるフレーズ", "使えそうな表現", "useful phrase", "useful phrases", "native expression", "native expressions", "phrases to remember")):
        return "phrase_recommendation"
    if _contains_any(query, ("弱点", "ミス", "苦手", "mistake", "mistakes", "weakness", "weaknesses")):
        return "weakness"
    if _contains_any(query, ("良かったところ", "できるようになった", "成長", "strength", "strengths", "good point", "good points")):
        return "strength"
    return "general_history"


def _requested_count(query: str) -> int:
    match = re.search(r"(?<!\d)(\d{1,3})\s*(?:個|件|phrases?|expressions?)", query, re.IGNORECASE)
    if match is None:
        match = re.search(r"\bgive\s+me\s+(\d{1,3})\b", query, re.IGNORECASE)
    if match is None:
        return DEFAULT_RESULT_COUNT
    return min(int(match.group(1)), MAX_REQUESTED_RESULTS)


def _has_requested_count(query: str) -> bool:
    return _requested_count(query) != DEFAULT_RESULT_COUNT or bool(
        re.search(r"(?<!\d)5\s*(?:個|件|phrases?|expressions?)|\bgive\s+me\s+5\b", query, re.IGNORECASE)
    )


def _contains_any(query: str, markers: tuple[str, ...]) -> bool:
    return any(marker in query for marker in markers)


def _date_range(query: str, reference_date: date | None) -> tuple[tuple[date, date] | None, bool]:
    explicit = _explicit_year_month(query)
    if explicit is not None:
        return _month_range(*explicit), False

    # A named month is more specific than any relative-period wording in the same query.
    month = _yearless_month(query)
    if month is not None:
        if reference_date is None:
            return None, True
        year = reference_date.year if month <= reference_date.month else reference_date.year - 1
        return _month_range(year, month), False

    if _contains_any(query, ("今月", "this month", "先月", "last month")):
        if reference_date is None:
            return None, True
        if _contains_any(query, ("先月", "last month")):
            year, month = _previous_month(reference_date.year, reference_date.month)
            return _month_range(year, month), False
        return _month_range(reference_date.year, reference_date.month), False

    relative_period = _relative_period(query)
    if relative_period is None:
        return None, False
    if reference_date is None:
        return None, True
    amount, unit = relative_period
    if unit == "months":
        return _relative_month_range(reference_date, amount), False
    return (reference_date - _relative_period_delta(amount, unit), reference_date), False


def _explicit_year_month(query: str) -> tuple[int, int] | None:
    match = re.search(r"\b(20\d{2})\s*(?:年|/|-)\s*(0?[1-9]|1[0-2])(?:月)?", query)
    if match is not None:
        return int(match.group(1)), int(match.group(2))
    month_names = "january february march april may june july august september october november december".split()
    for month, name in enumerate(month_names, start=1):
        match = re.search(rf"\b{name}\s+(20\d{{2}})\b", query)
        if match is not None:
            return int(match.group(1)), month
    return None


def _yearless_month(query: str) -> int | None:
    match = re.search(r"(?<!\d)(1[0-2]|[1-9])月", query)
    if match is not None:
        return int(match.group(1))
    month_names = "january february march april may june july august september october november december".split()
    for month, name in enumerate(month_names, start=1):
        if re.search(rf"\bin\s+{name}\b", query):
            return month
    return None


def _relative_period(query: str) -> tuple[int, str] | None:
    """Parse numeric relative periods that must become a hard date range."""
    japanese = re.search(r"(?:最近|直近|過去)\s*(\d{1,3})\s*(週間|日|ヶ月|か月|月)", query)
    if japanese is not None:
        amount = int(japanese.group(1))
        unit = japanese.group(2)
        return amount, {"週間": "weeks", "日": "days", "ヶ月": "months", "か月": "months", "月": "months"}[unit]

    english = re.search(r"\b(?:last|past)\s+(\d{1,3})\s+(weeks?|days?|months?)\b", query)
    if english is not None:
        amount = int(english.group(1))
        unit = english.group(2).rstrip("s")
        return amount, f"{unit}s"
    return None


def _relative_period_delta(amount: int, unit: str) -> timedelta:
    if unit == "weeks":
        return timedelta(days=max(amount * 7 - 1, 0))
    return timedelta(days=max(amount - 1, 0))


def _relative_month_range(reference_date: date, months: int) -> tuple[date, date]:
    """Use a calendar-month offset while keeping the reference date as the inclusive end."""
    total_months = reference_date.year * 12 + reference_date.month - 1 - months
    year, month_index = divmod(total_months, 12)
    month = month_index + 1
    start_day = min(reference_date.day, monthrange(year, month)[1])
    return date(year, month, start_day), reference_date


def _month_range(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, monthrange(year, month)[1])


def _previous_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)
