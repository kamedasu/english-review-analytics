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


@dataclass(frozen=True)
class ReviewTargetSummary:
    phrase: str
    first_seen_date: str
    last_seen_date: str
    occurrence_count: int
    highest_priority: str
    meanings: list[str]
    examples: list[str]
    reused_count: int
    matched_fields: list[str]
    review_status: str


def detect_reused_phrases(reviews: list[Review]) -> list[ReusedPhrase]:
    sorted_reviews = sorted(reviews, key=lambda review: review.date)
    results: list[ReusedPhrase] = []
    first_seen = _first_seen_by_phrase(sorted_reviews)

    for source_index, review in enumerate(sorted_reviews):
        source_candidates = _review_target_candidates(review)
        for phrase, source_field in source_candidates.items():
            first = first_seen.get(phrase)
            if not first:
                continue
            for later_review in sorted_reviews[source_index + 1 :]:
                match_field = _find_phrase_in_review(phrase, later_review)
                if match_field:
                    results.append(
                        ReusedPhrase(
                            phrase=first["phrase"],
                            first_review_id=first["review_id"],
                            reused_review_id=later_review.review_id,
                            reused_on=later_review.date.isoformat(),
                            matched_field=match_field,
                        )
                    )
                    break
    return results


def summarize_review_targets(base_reviews: list[Review], history_reviews: list[Review] | None = None) -> list[ReviewTargetSummary]:
    sorted_base_reviews = sorted(base_reviews, key=lambda review: review.date)
    sorted_history_reviews = sorted(history_reviews if history_reviews is not None else base_reviews, key=lambda review: review.date)
    grouped: dict[str, dict] = {}

    for review in sorted_base_reviews:
        for phrase, source_field in _review_target_candidates(review).items():
            key = normalize_phrase(phrase)
            if key not in grouped:
                grouped[key] = {
                    "phrase": phrase,
                    "first_seen_date": review.date.isoformat(),
                    "last_seen_date": review.date.isoformat(),
                    "occurrence_dates": set(),
                    "reuse_dates": set(),
                    "matched_fields": set(),
                    "highest_priority": "",
                    "meanings": set(),
                    "examples": set(),
                }

            item = grouped[key]
            item["occurrence_dates"].add(review.date.isoformat())
            item["last_seen_date"] = max(item["last_seen_date"], review.date.isoformat())
            item["matched_fields"].add(source_field)
            _add_phrase_card_metadata(item, review, key)

            for later_review in sorted_history_reviews:
                if later_review.date <= review.date:
                    continue
                matched_fields = _find_phrase_fields_in_review(key, later_review)
                if not matched_fields:
                    continue
                item["reuse_dates"].add(later_review.date.isoformat())
                item["occurrence_dates"].add(later_review.date.isoformat())
                item["last_seen_date"] = max(item["last_seen_date"], later_review.date.isoformat())
                item["matched_fields"].update(matched_fields)
                _add_phrase_card_metadata(item, later_review, key)

    summaries = [
        ReviewTargetSummary(
            phrase=item["phrase"],
            first_seen_date=item["first_seen_date"],
            last_seen_date=item["last_seen_date"],
            occurrence_count=len(item["occurrence_dates"]),
            highest_priority=item["highest_priority"],
            meanings=sorted(item["meanings"]),
            examples=sorted(item["examples"]),
            reused_count=len(item["reuse_dates"]),
            matched_fields=sorted(item["matched_fields"]),
            review_status=_retention_label(len(item["reuse_dates"]), sorted(item["matched_fields"])),
        )
        for item in grouped.values()
    ]
    return sorted(
        summaries,
        key=lambda item: (item.reused_count, item.occurrence_count, item.last_seen_date, item.phrase.lower()),
        reverse=True,
    )


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
        for phrase in _review_target_candidates(review):
            key = normalize_phrase(phrase)
            if key and key not in first_seen:
                first_seen[key] = {
                    "phrase": phrase,
                    "date": review.date.isoformat(),
                    "review_id": review.review_id,
                }
    return first_seen


def _review_target_candidates(review: Review) -> dict[str, str]:
    candidates: dict[str, str] = {}
    for card in getattr(review, "phrase_cards", []) or []:
        _add_candidate(candidates, card.phrase, "phrase_card")
    for phrase in getattr(review, "expressions_to_add", []) or []:
        _add_candidate(candidates, phrase, "expressions_to_add")
    for phrase in getattr(review, "expressions_to_use_next_time", []) or []:
        _add_candidate(candidates, phrase, "expressions_to_use_next_time")
    return candidates


def _add_candidate(candidates: dict[str, str], phrase: str, field_name: str) -> None:
    key = normalize_phrase(phrase or "")
    if key and key not in candidates:
        candidates[key] = field_name


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


def _add_phrase_card_metadata(item: dict, review: Review, phrase_key: str) -> None:
    for card in getattr(review, "phrase_cards", []) or []:
        if normalize_phrase(card.phrase) != phrase_key:
            continue
        item["highest_priority"] = _higher_priority(item["highest_priority"], card.priority)
        if card.meaning:
            item["meanings"].add(card.meaning)
        if card.example:
            item["examples"].add(card.example)


def _higher_priority(current: str, candidate: str) -> str:
    priority_rank = {"High": 3, "Medium": 2, "Low": 1}
    return candidate if priority_rank.get(candidate, 0) > priority_rank.get(current, 0) else current


def _find_phrase_in_review(phrase: str, review: Review) -> str | None:
    fields = _find_phrase_fields_in_review(phrase, review)
    return fields[0] if fields else None


def _find_phrase_fields_in_review(phrase: str, review: Review) -> list[str]:
    fields = {
        "phrase_card": " ".join(card.phrase for card in (getattr(review, "phrase_cards", []) or [])),
        "expressions_to_add": " ".join(getattr(review, "expressions_to_add", []) or []),
        "expressions_to_use_next_time": " ".join(getattr(review, "expressions_to_use_next_time", []) or []),
        "actually_used": " ".join(getattr(review, "words_and_phrases_actually_used", []) or []),
    }
    matched_fields: list[str] = []
    for field_name, text in fields.items():
        if phrase in normalize_phrase(text):
            matched_fields.append(field_name)
    return matched_fields
