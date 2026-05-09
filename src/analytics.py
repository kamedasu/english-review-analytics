from __future__ import annotations

from collections import defaultdict

import pandas as pd

from src.models import PhraseCard, Review, StudySummary
from src.reuse_detector import detect_reused_phrases, normalize_phrase
from src.streak import longest_streak
from src.utils.dates import month_key


def available_months(reviews: list[Review]) -> list[str]:
    return sorted({month_key(review.date) for review in reviews}, reverse=True)


def available_quarters(reviews: list[Review]) -> list[str]:
    return sorted({quarter_key(review) for review in reviews}, reverse=True)


def available_years(reviews: list[Review]) -> list[str]:
    return sorted({year_key(review) for review in reviews}, reverse=True)


def filter_reviews_by_month(reviews: list[Review], month: str) -> list[Review]:
    return [review for review in reviews if month_key(review.date) == month]


def filter_reviews_by_quarter(reviews: list[Review], quarter: str) -> list[Review]:
    return [review for review in reviews if quarter_key(review) == quarter]


def filter_reviews_by_year(reviews: list[Review], year: str) -> list[Review]:
    return [review for review in reviews if year_key(review) == year]


def summarize_month(reviews: list[Review], month: str) -> StudySummary:
    return summarize_reviews(filter_reviews_by_month(reviews, month), month)


def summarize_quarter(reviews: list[Review], quarter: str) -> StudySummary:
    return summarize_reviews(filter_reviews_by_quarter(reviews, quarter), quarter)


def summarize_year(reviews: list[Review], year: str) -> StudySummary:
    return summarize_reviews(filter_reviews_by_year(reviews, year), year)


def summarize_reviews(period_reviews: list[Review], period_label: str) -> StudySummary:
    reused = detect_reused_phrases(period_reviews)
    phrase_count = sum(len(review.phrase_cards) for review in period_reviews)
    return StudySummary(
        month=period_label,
        total_duration_minutes=sum(review.duration_minutes for review in period_reviews),
        study_days=len({review.date for review in period_reviews}),
        longest_streak=longest_streak([review.date for review in period_reviews]),
        review_count=len(period_reviews),
        phrase_count=phrase_count,
        reused_phrase_count=len(reused),
    )


def quarter_key(review: Review) -> str:
    quarter = ((review.date.month - 1) // 3) + 1
    return f"{review.date.year}-Q{quarter}"


def year_key(review: Review) -> str:
    return str(review.date.year)


def phrase_cards_for_reviews(reviews: list[Review]) -> list[PhraseCard]:
    cards: list[PhraseCard] = []
    for review in reviews:
        cards.extend(review.phrase_cards)
    return cards


def reviews_to_dataframe(reviews: list[Review]) -> pd.DataFrame:
    rows = [
        {
            "date": review.date.isoformat(),
            "duration_minutes": review.duration_minutes,
            "topic": review.topic,
            "good_points": "\n".join(review.good_points),
            "expressions_to_add": "\n".join(review.expressions_to_add),
            "expressions_to_use_next_time": "\n".join(review.expressions_to_use_next_time),
            "comment": review.comment,
            "phrase_count": len(review.phrase_cards),
            "source_page_title": review.source_page_title,
        }
        for review in sorted(reviews, key=lambda item: item.date, reverse=True)
    ]
    return pd.DataFrame(rows)


def phrases_to_dataframe(cards: list[PhraseCard]) -> pd.DataFrame:
    rows = [
        {
            "phrase": card.phrase,
            "meaning": card.meaning,
            "example": card.example,
            "next_review_date": card.next_review_date.isoformat() if card.next_review_date else "",
            "priority": card.priority,
            "source_review_date": card.source_review_date.isoformat() if card.source_review_date else "",
        }
        for card in cards
    ]
    return pd.DataFrame(rows)


def dedupe_phrases_to_dataframe(cards: list[PhraseCard]) -> pd.DataFrame:
    grouped: dict[str, dict] = {}
    for card in cards:
        key = normalize_phrase(card.phrase)
        if not key:
            continue
        seen_date = card.source_review_date
        if key not in grouped:
            grouped[key] = {
                "phrase": card.phrase,
                "first_seen_date": seen_date,
                "last_seen_date": seen_date,
                "occurrence_count": 0,
                "highest_priority": card.priority,
                "meanings": set(),
                "examples": set(),
            }
        item = grouped[key]
        item["occurrence_count"] += 1
        item["highest_priority"] = _higher_priority(item["highest_priority"], card.priority)
        if seen_date and (item["first_seen_date"] is None or seen_date < item["first_seen_date"]):
            item["first_seen_date"] = seen_date
        if seen_date and (item["last_seen_date"] is None or seen_date > item["last_seen_date"]):
            item["last_seen_date"] = seen_date
        if card.meaning:
            item["meanings"].add(card.meaning)
        if card.example:
            item["examples"].add(card.example)

    rows = []
    for item in grouped.values():
        rows.append(
            {
                "phrase": item["phrase"],
                "first_seen_date": item["first_seen_date"].isoformat() if item["first_seen_date"] else "",
                "last_seen_date": item["last_seen_date"].isoformat() if item["last_seen_date"] else "",
                "occurrence_count": item["occurrence_count"],
                "highest_priority": item["highest_priority"],
                "meanings": "\n".join(sorted(item["meanings"])),
                "examples": "\n".join(sorted(item["examples"])),
            }
        )
    return pd.DataFrame(
        sorted(rows, key=lambda row: (row["occurrence_count"], row["last_seen_date"], row["phrase"]), reverse=True)
    )


def duration_by_day(reviews: list[Review]) -> pd.DataFrame:
    grouped: dict[str, int] = defaultdict(int)
    for review in reviews:
        grouped[review.date.isoformat()] += review.duration_minutes
    return pd.DataFrame(
        [{"date": date, "duration_minutes": minutes} for date, minutes in sorted(grouped.items())]
    )


def _higher_priority(current: str, candidate: str) -> str:
    priority_rank = {"High": 3, "Medium": 2, "Low": 1}
    current_rank = priority_rank.get(current, 0)
    candidate_rank = priority_rank.get(candidate, 0)
    return candidate if candidate_rank > current_rank else current
