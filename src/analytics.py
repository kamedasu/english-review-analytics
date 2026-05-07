from __future__ import annotations

from collections import defaultdict

import pandas as pd

from src.models import PhraseCard, Review, StudySummary
from src.reuse_detector import detect_reused_phrases
from src.streak import longest_streak
from src.utils.dates import month_key


def available_months(reviews: list[Review]) -> list[str]:
    return sorted({month_key(review.date) for review in reviews}, reverse=True)


def filter_reviews_by_month(reviews: list[Review], month: str) -> list[Review]:
    return [review for review in reviews if month_key(review.date) == month]


def summarize_month(reviews: list[Review], month: str) -> StudySummary:
    monthly_reviews = filter_reviews_by_month(reviews, month)
    reused = detect_reused_phrases(monthly_reviews)
    phrase_count = sum(len(review.phrase_cards) for review in monthly_reviews)
    return StudySummary(
        month=month,
        total_duration_minutes=sum(review.duration_minutes for review in monthly_reviews),
        study_days=len({review.date for review in monthly_reviews}),
        longest_streak=longest_streak([review.date for review in monthly_reviews]),
        review_count=len(monthly_reviews),
        phrase_count=phrase_count,
        reused_phrase_count=len(reused),
    )


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


def duration_by_day(reviews: list[Review]) -> pd.DataFrame:
    grouped: dict[str, int] = defaultdict(int)
    for review in reviews:
        grouped[review.date.isoformat()] += review.duration_minutes
    return pd.DataFrame(
        [{"date": date, "duration_minutes": minutes} for date, minutes in sorted(grouped.items())]
    )
