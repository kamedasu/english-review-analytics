from __future__ import annotations

from dataclasses import dataclass

from src.models import PhraseCard, Review


@dataclass(frozen=True)
class RagChunk:
    chunk_id: str
    text: str
    metadata: dict[str, str]


def review_to_chunks(review: Review) -> list[RagChunk]:
    chunks = [
        RagChunk(
            chunk_id=f"{review.review_id}:review",
            text=review.raw_markdown,
            metadata={
                "type": "review",
                "review_id": review.review_id,
                "date": review.date.isoformat(),
                "source_page_id": review.source_page_id,
            },
        )
    ]
    for index, card in enumerate(review.phrase_cards, start=1):
        chunks.append(phrase_to_chunk(card, review.review_id, index))
    return chunks


def phrase_to_chunk(card: PhraseCard, review_id: str, index: int) -> RagChunk:
    text = "\n".join(
        [
            f"Phrase: {card.phrase}",
            f"Meaning: {card.meaning}",
            f"Example: {card.example}",
            f"Priority: {card.priority}",
        ]
    )
    return RagChunk(
        chunk_id=f"{review_id}:phrase:{index}",
        text=text,
        metadata={
            "type": "phrase",
            "review_id": review_id,
            "source_review_date": card.source_review_date.isoformat() if card.source_review_date else "",
        },
    )
