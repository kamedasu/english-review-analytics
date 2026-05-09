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


@dataclass(frozen=True)
class PhraseReuseSummary:
    phrase: str
    first_seen_date: str
    reused_dates: list[str]
    reuse_count: int
    matched_fields: list[str]
    last_used_date: str
    retention_label: str


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


def summarize_reused_phrases(reviews: list[Review]) -> list[PhraseReuseSummary]:
    sorted_reviews = sorted(reviews, key=lambda review: review.date)
    raw_reuse = detect_reused_phrases(sorted_reviews)
    first_seen = _first_seen_by_phrase(sorted_reviews)
    grouped: dict[str, dict] = {}

    for item in raw_reuse:
        key = normalize_phrase(item.phrase)
        if key not in grouped:
            grouped[key] = {
                "phrase": first_seen.get(key, {}).get("phrase", item.phrase),
                "first_seen_date": first_seen.get(key, {}).get("date", ""),
                "reused_dates": set(),
                "matched_fields": set(),
            }
        grouped[key]["reused_dates"].add(item.reused_on)
        grouped[key]["matched_fields"].add(item.matched_field)

    summaries: list[PhraseReuseSummary] = []
    for key, data in grouped.items():
        reused_dates = sorted(data["reused_dates"])
        matched_fields = sorted(data["matched_fields"])
        first_seen_date = data["first_seen_date"]
        last_used_date = reused_dates[-1] if reused_dates else first_seen_date
        reuse_count = len(reused_dates)
        summaries.append(
            PhraseReuseSummary(
                phrase=data["phrase"],
                first_seen_date=first_seen_date,
                reused_dates=reused_dates,
                reuse_count=reuse_count,
                matched_fields=matched_fields,
                last_used_date=last_used_date,
                retention_label=_retention_label(reuse_count, matched_fields),
            )
        )

    return sorted(
        summaries,
        key=lambda item: (item.reuse_count, item.last_used_date, item.phrase.lower()),
        reverse=True,
    )


def normalize_phrase(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"\s+", " ", value)
    return value.strip("\"'`.,!?;:()[]{}")


def _first_seen_by_phrase(reviews: list[Review]) -> dict[str, dict[str, str]]:
    first_seen: dict[str, dict[str, str]] = {}
    for review in reviews:
        for card in review.phrase_cards:
            key = normalize_phrase(card.phrase)
            if key and key not in first_seen:
                first_seen[key] = {
                    "phrase": card.phrase,
                    "date": review.date.isoformat(),
                }
    return first_seen


def _retention_label(reuse_count: int, matched_fields: list[str]) -> str:
    if reuse_count <= 0:
        return "New"
    if reuse_count == 1:
        return "Reused"
    if reuse_count == 2:
        return "Retained"
    if reuse_count >= 3:
        return "Strong"
    return "New"


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
