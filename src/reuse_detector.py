from __future__ import annotations

import re
from dataclasses import dataclass

from src.models import Review


@dataclass(frozen=True)
class ReusedPhrase:
    phrase: str
    first_review_id: str
    reused_review_id: str
    reused_on: str
    matched_field: str


def detect_reused_phrases(reviews: list[Review]) -> list[ReusedPhrase]:
    sorted_reviews = sorted(reviews, key=lambda review: review.date)
    results: list[ReusedPhrase] = []

    for source_index, review in enumerate(sorted_reviews):
        for card in review.phrase_cards:
            phrase = normalize_phrase(card.phrase)
            if not phrase:
                continue
            for later_review in sorted_reviews[source_index + 1 :]:
                match_field = _find_phrase_in_review(phrase, later_review)
                if match_field:
                    results.append(
                        ReusedPhrase(
                            phrase=card.phrase,
                            first_review_id=review.review_id,
                            reused_review_id=later_review.review_id,
                            reused_on=later_review.date.isoformat(),
                            matched_field=match_field,
                        )
                    )
                    break
    return results


def normalize_phrase(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"\s+", " ", value)
    return value.strip("\"'`.,!?;:()[]{}")


def _find_phrase_in_review(phrase: str, review: Review) -> str | None:
    fields = {
        "topic": review.topic,
        "comment": review.comment,
        "good_points": " ".join(review.good_points),
        "expressions_to_add": " ".join(review.expressions_to_add),
        "expressions_to_use_next_time": " ".join(review.expressions_to_use_next_time),
    }
    for field_name, text in fields.items():
        if phrase in normalize_phrase(text):
            return field_name
    return None
