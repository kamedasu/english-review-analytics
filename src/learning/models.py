"""Immutable recorded facts. No inferred difficulty, correctness or mastery state."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

CATALOG_SCHEMA_VERSION = '1'


class EvidenceType(StrEnum):
    PHRASE_CARD = 'phrase_card'
    CORRECTION_RECORD = 'correction_record'
    EXPLICIT_USAGE_RECORD = 'explicit_usage_record'
    SUGGESTED_EXPRESSION = 'suggested_expression'
    NEXT_PRACTICE_EXPRESSION = 'next_practice_expression'
    WEAKNESS_RECORD = 'weakness_record'
    POSITIVE_FEEDBACK = 'positive_feedback'
    DRAFT_RECORD = 'draft_record'
    CORRECTED_VERSION_RECORD = 'corrected_version_record'
    CORRECTION_EXPLANATION = 'correction_explanation'
    CONTEXT_RECORD = 'context_record'


@dataclass(frozen=True)
class ExpressionLink:
    expression_id: str
    expression: str  # Exact source substring/value, before normalization.
    role: str  # value, quoted, original, corrected, note_quoted


@dataclass(frozen=True)
class LearningEvidence:
    evidence_id: str
    review_id: str
    review_date: date
    topic: str
    review_type: str
    source_page_id: str
    source_block_id: str
    evidence_type: EvidenceType
    source_field: str
    source_index: int
    original_text: str
    corrected_text: str = ''
    note: str = ''
    links: tuple[ExpressionLink, ...] = ()
    # Unstructured/empty source records are retained, not silently discarded.
    extraction_issue: str | None = None
    source_metadata: tuple[tuple[str, str], ...] = ()

    @property
    def expression_ids(self) -> tuple[str, ...]:
        return tuple(sorted({link.expression_id for link in self.links}))


@dataclass(frozen=True)
class ExpressionStatistics:
    expression_id: str
    unique_review_count: int
    record_count: int
    # Explicit learning-target fields only, excluding usage/corrections/feedback.
    learning_record_count: int
    correction_record_count: int
    explicit_usage_record_count: int
    weakness_record_count: int
    first_seen: date | None
    last_seen: date | None
    last_correction_date: date | None
    last_usage_date: date | None
    counts_by_type: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class Expression:
    expression_id: str
    expression: str
    normalized_expression: str
    original_variants: tuple[str, ...]
    meanings: tuple[str, ...]
    examples: tuple[str, ...]
    topics: tuple[str, ...]
    first_seen: date
    last_seen: date
    occurrence_count: int  # Unique review IDs, not number of records or dates.
    evidence_ids: tuple[str, ...]
    statistics: ExpressionStatistics
