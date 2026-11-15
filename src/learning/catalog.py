"""Rebuildable in-memory catalog. No disk writes, automatic API calls or ranking."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
import hashlib
import json
from typing import TYPE_CHECKING, Iterable, Mapping

from src.models import Review
from src.learning.evidence import EXTRACTOR_VERSION, extract_evidence
from src.learning.models import (CATALOG_SCHEMA_VERSION, EvidenceType, Expression,
                                  ExpressionStatistics, LearningEvidence)
from src.learning.normalization import NORMALIZATION_VERSION, expression_id, normalize_expression

if TYPE_CHECKING:
    from src.storage.base import Storage

_LEARNING_TYPES = {EvidenceType.PHRASE_CARD, EvidenceType.SUGGESTED_EXPRESSION,
                   EvidenceType.NEXT_PRACTICE_EXPRESSION}


def _date_range(start_date, end_date):
    if start_date is not None and end_date is not None and start_date > end_date:
        raise ValueError('start_date must not be after end_date')


def _statistics(key: str, records: tuple[LearningEvidence, ...]) -> ExpressionStatistics:
    counts = Counter(record.evidence_type for record in records)
    dates = [r.review_date for r in records]
    corrections = [r.review_date for r in records if r.evidence_type == EvidenceType.CORRECTION_RECORD]
    usages = [r.review_date for r in records if r.evidence_type == EvidenceType.EXPLICIT_USAGE_RECORD]
    return ExpressionStatistics(
        expression_id=key, unique_review_count=len({r.review_id for r in records}),
        record_count=len(records), learning_record_count=sum(counts[kind] for kind in _LEARNING_TYPES),
        correction_record_count=counts[EvidenceType.CORRECTION_RECORD],
        explicit_usage_record_count=counts[EvidenceType.EXPLICIT_USAGE_RECORD],
        weakness_record_count=counts[EvidenceType.WEAKNESS_RECORD],
        first_seen=min(dates, default=None), last_seen=max(dates, default=None),
        last_correction_date=max(corrections, default=None), last_usage_date=max(usages, default=None),
        counts_by_type=tuple(sorted((str(kind), count) for kind, count in counts.items())),
    )


class ExpressionCatalog:
    """Read-only views reaggregate filtered evidence, never reuse all-time totals."""
    schema_version = CATALOG_SCHEMA_VERSION
    normalization_version = NORMALIZATION_VERSION
    extractor_version = EXTRACTOR_VERSION

    def __init__(self, evidence: Iterable[LearningEvidence], *, source_data_hash: str):
        self.evidence = tuple(sorted(evidence, key=lambda r: (r.review_date, r.review_id, r.source_field,
                                                            r.source_index, r.evidence_id)))
        self.source_data_hash = source_data_hash
        grouped = defaultdict(list)
        for record in self.evidence:
            for key in record.expression_ids:
                grouped[key].append(record)  # One correction record may link both sides; count once per expression.
        self._history = {key: tuple(records) for key, records in grouped.items()}
        self._expressions = {}
        for key, records in self._history.items():
            variants = tuple(sorted({link.expression for r in records for link in r.links if link.expression_id == key}))
            metadata = [dict(r.source_metadata) for r in records if r.evidence_type == EvidenceType.PHRASE_CARD]
            stats = _statistics(key, records)
            self._expressions[key] = Expression(
                expression_id=key, expression=variants[0], normalized_expression=normalize_expression(variants[0]),
                original_variants=variants,
                meanings=tuple(sorted({m['meaning'] for m in metadata if m.get('meaning')})),
                examples=tuple(sorted({m['example'] for m in metadata if m.get('example')})),
                topics=tuple(sorted({r.topic for r in records if r.topic})),
                first_seen=stats.first_seen, last_seen=stats.last_seen,
                occurrence_count=stats.unique_review_count, evidence_ids=tuple(r.evidence_id for r in records),
                statistics=stats,
            )

    def all(self) -> tuple[Expression, ...]:
        return tuple(sorted(self._expressions.values(), key=lambda e: e.normalized_expression))

    def get(self, key: str) -> Expression | None:
        return self._expressions.get(key)

    def find(self, expression: str) -> Expression | None:
        if not normalize_expression(expression):
            return None
        return self.get(expression_id(expression))

    @property
    def unlinked_evidence(self) -> tuple[LearningEvidence, ...]:
        return tuple(record for record in self.evidence if not record.links)

    def select(self, *, start_date: date | None = None, end_date: date | None = None,
               topic: str | None = None,
               evidence_types: Iterable[EvidenceType | str] | None = None) -> ExpressionCatalog:
        """Inclusive dates, literal case-insensitive topic substring, exact evidence types.

        Unlinked review evidence participates in these filters as well. Meanings,
        variants, dates and counts are all rebuilt only from the selected evidence.
        """
        _date_range(start_date, end_date)
        kinds = None if evidence_types is None else {EvidenceType(kind) for kind in evidence_types}
        topic_key = None if topic is None else normalize_expression(topic)
        return ExpressionCatalog((r for r in self.evidence
                                  if (start_date is None or r.review_date >= start_date)
                                  and (end_date is None or r.review_date <= end_date)
                                  and (topic_key is None or topic_key in normalize_expression(r.topic))
                                  and (kinds is None or r.evidence_type in kinds)),
                                 source_data_hash=self.source_data_hash)

    def history(self, key: str, *, start_date: date | None = None,
                end_date: date | None = None) -> tuple[LearningEvidence, ...]:
        _date_range(start_date, end_date)
        return tuple(r for r in self._history.get(key, ())
                     if (start_date is None or r.review_date >= start_date)
                     and (end_date is None or r.review_date <= end_date))

    def statistics(self, key: str, *, start_date: date | None = None,
                   end_date: date | None = None) -> ExpressionStatistics:
        # Unknown IDs and known expressions absent from a period both have zero
        # evidence; get() distinguishes whether a key exists in this view.
        return _statistics(key, self.history(key, start_date=start_date, end_date=end_date))

    def search(self, *, start_date: date | None = None, end_date: date | None = None,
               topic: str | None = None, evidence_types: Iterable[EvidenceType | str] | None = None,
               last_seen_after: date | None = None, last_seen_before: date | None = None,
               last_correction_after: date | None = None, last_correction_before: date | None = None,
               last_usage_after: date | None = None, last_usage_before: date | None = None,
               has_usage: bool | None = None) -> tuple[Expression, ...]:
        """Dates in *_after / *_before are inclusive. Missing dates never match a bound.

        Last-date criteria apply AFTER evidence scoping, so period searches have
        period-specific dates/counts. No semantic search or mastery inference.
        """
        for low, high in [(last_seen_after, last_seen_before),
                          (last_correction_after, last_correction_before),
                          (last_usage_after, last_usage_before)]:
            _date_range(low, high)
        view = self.select(start_date=start_date, end_date=end_date, topic=topic, evidence_types=evidence_types)
        result = []
        for item in view.all():
            stats = item.statistics
            if has_usage is not None and bool(stats.explicit_usage_record_count) != has_usage:
                continue
            bounds = [(stats.last_seen, last_seen_after, last_seen_before),
                      (stats.last_correction_date, last_correction_after, last_correction_before),
                      (stats.last_usage_date, last_usage_after, last_usage_before)]
            if all((low is None and high is None) or
                   (value is not None and (low is None or value >= low) and (high is None or value <= high))
                   for value, low, high in bounds):
                result.append(item)
        return tuple(result)


def build_catalog(reviews: Iterable[Review | Mapping]) -> ExpressionCatalog:
    # Copies/validates input without changing the source Review or its nested cards.
    rows = [Review.model_validate(r) if not isinstance(r, Review) else r for r in reviews]
    if len({r.review_id for r in rows}) != len(rows):
        raise ValueError('Duplicate review IDs: catalog construction stopped')
    rows.sort(key=lambda r: r.review_id)
    payload = json.dumps([r.model_dump(mode='json') for r in rows], ensure_ascii=False,
                         sort_keys=True, separators=(',', ':'))
    source_hash = hashlib.sha256(payload.encode()).hexdigest()
    return ExpressionCatalog((record for r in rows for record in extract_evidence(r)), source_data_hash=source_hash)


def load_catalog(storage: Storage) -> ExpressionCatalog:
    """Explicit read-only access via existing Local/S3 abstraction. No default backend.

    No persistent cache: every call rebuilds from the processed source of truth.
    Caller controls execution/caching; importing this package performs no I/O.
    """
    path = 'processed/reviews.json'
    if not storage.exists(path):
        return build_catalog([])
    payload = storage.load_json(path)
    if not isinstance(payload, list):
        raise ValueError('Processed reviews must be a list')
    return build_catalog(payload)
