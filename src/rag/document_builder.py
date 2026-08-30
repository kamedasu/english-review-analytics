from __future__ import annotations

from datetime import date, datetime
import hashlib
from typing import Any

from src.models import Review
from src.rag.models import RagDocument, RagMetadataValue


def build_rag_documents(reviews: list[Review]) -> list[RagDocument]:
    """Build one retrieval document for each meaningful review item.

    This intentionally excludes ``Review.raw_markdown``: retrieval is based on
    structured learning units rather than an entire review.
    """
    documents: list[RagDocument] = []
    used_ids: set[str] = set()

    for review_index, review in enumerate(reviews):
        for item_index, value in enumerate(review.good_points):
            value = _text(value)
            if value:
                documents.append(
                    _document(
                        review, review_index, "good_point", item_index,
                        f"Good point: {value}", used_ids,
                    )
                )

        for item_index, value in enumerate(review.weak_points):
            value = _text(value)
            if value:
                documents.append(
                    _document(
                        review, review_index, "weak_point", item_index,
                        f"Weak point: {value}", used_ids,
                    )
                )

        for item_index, expression in enumerate(review.more_natural_expressions):
            fields = [
                ("Your phrase", _text(getattr(expression, "your_phrase", ""))),
                ("More natural", _text(getattr(expression, "more_natural", ""))),
                ("Note", _text(getattr(expression, "note", ""))),
            ]
            text = _labeled_text(fields)
            if text:
                documents.append(
                    _document(
                        review, review_index, "more_natural_expression", item_index, text, used_ids
                    )
                )

        for item_index, card in enumerate(review.phrase_cards):
            phrase = _text(getattr(card, "phrase", ""))
            if not phrase:
                continue
            text = _labeled_text(
                [
                    ("Phrase", phrase),
                    ("Meaning", _text(getattr(card, "meaning", ""))),
                    ("Example", _text(getattr(card, "example", ""))),
                ]
            )
            metadata = _common_metadata(review, "phrase_card", item_index)
            for field in ("source", "priority", "review_status", "next_review_date", "source_review_date"):
                if hasattr(card, field):
                    metadata[field] = _metadata_value(getattr(card, field))
            documents.append(
                _document(
                    review, review_index, "phrase_card", item_index, text, used_ids, metadata
                )
            )

    return documents


def _document(
    review: Review,
    review_index: int,
    document_type: str,
    item_index: int,
    text: str,
    used_ids: set[str],
    metadata: dict[str, RagMetadataValue] | None = None,
) -> RagDocument:
    document_id = _unique_id(
        review, review_index, document_type, item_index, text, used_ids
    )
    return RagDocument(
        id=document_id,
        text=text,
        metadata=metadata or _common_metadata(review, document_type, item_index),
    )


def _common_metadata(
    review: Review, document_type: str, item_index: int
) -> dict[str, RagMetadataValue]:
    metadata: dict[str, RagMetadataValue] = {
        "type": document_type,
        "review_id": _text(getattr(review, "review_id", "")),
        "date": review.date.isoformat(),
        "year": review.date.year,
        "month": review.date.strftime("%Y-%m"),
        "topic": _text(getattr(review, "topic", "")),
        "review_type": _text(getattr(review, "review_type", "")),
        "item_index": item_index,
    }
    source_page_id = _text(getattr(review, "source_page_id", ""))
    if source_page_id:
        metadata["source_page_id"] = source_page_id
    return metadata


def _unique_id(
    review: Review,
    review_index: int,
    document_type: str,
    item_index: int,
    text: str,
    used_ids: set[str],
) -> str:
    review_id = _text(getattr(review, "review_id", ""))
    if not review_id:
        fingerprint = "|".join(
            [review.date.isoformat(), _text(review.topic), _text(review.review_type), str(review_index)]
        )
        review_id = f"review-{hashlib.sha256(fingerprint.encode()).hexdigest()[:16]}"
    candidate = f"{review_id}:{document_type}:{item_index}"
    if candidate in used_ids:
        # Duplicate review IDs are unexpected, but must not create duplicate RAG IDs.
        suffix = hashlib.sha256(f"{review_index}|{text}".encode()).hexdigest()[:12]
        candidate = f"{candidate}:{suffix}"
    used_ids.add(candidate)
    return candidate


def _labeled_text(fields: list[tuple[str, str]]) -> str:
    return "\n".join(f"{label}: {value}" for label, value in fields if value)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _metadata_value(value: Any) -> RagMetadataValue:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)):
        return value
    return _text(value)
