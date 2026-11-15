"""Extract source records conservatively without network or semantic judgments."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import re
import unicodedata

from src.models import Review
from src.learning.models import EvidenceType, ExpressionLink, LearningEvidence
from src.learning.normalization import expression_id, normalize_expression

EXTRACTOR_VERSION = '1'
_QUOTES = re.compile(r'「([^」\n]+)」|『([^』\n]+)』|“([^”\n]+)”|"([^"\n]+)"|`([^`\n]+)`|(?<![A-Za-z])\x27([^\n]+?)\x27(?![A-Za-z])')


def _english_compatible(value: str) -> bool:
    # A surface eligibility check, NOT language identification or learning-value judgment.
    value = unicodedata.normalize('NFKC', value)
    return bool(re.search('[A-Za-z]', value) and normalize_expression(value)) and not any(
        unicodedata.category(c).startswith('L') and 'LATIN' not in unicodedata.name(c, '')
        for c in value)


def _links(text: str, role: str, *, prose: bool = False) -> tuple[ExpressionLink, ...]:
    # English-only structured fields are one recorded unit. Never mine words from
    # sentences or split slash alternatives without an explicit source structure.
    if not prose and _english_compatible(text):
        return (ExpressionLink(expression_id(text), text, role),)
    # Use the original text for exact offsets/variants. NFKC is only an eligibility
    # check below, not a replacement of recorded source text.
    values = [next(group for group in match.groups() if group is not None) for match in _QUOTES.finditer(text)]
    return tuple(ExpressionLink(expression_id(value), value, 'quoted' if prose else role)
                 for value in values if _english_compatible(unicodedata.normalize('NFKC', value)))


def extract_evidence(review: Review) -> tuple[LearningEvidence, ...]:
    result = []

    def add(kind, field, index, original, *, corrected='', note='', links=(), metadata=()):
        issue = None if links else ('empty_record' if not (original.strip() or corrected.strip() or note.strip())
                                    else 'no_unambiguous_expression')
        payload = dict(review_id=review.review_id, review_date=review.date, topic=review.topic,
                       review_type=review.review_type, source_page_id=review.source_page_id,
                       source_block_id=review.source_block_id, evidence_type=kind,
                       source_field=field, source_index=index, original_text=original,
                       corrected_text=corrected, note=note, links=links,
                       extraction_issue=issue, source_metadata=metadata)
        # Identical records at different source positions remain distinct. Review
        # order does not affect IDs; editing or moving a source item may change one.
        encoded = json.dumps({'extractor': EXTRACTOR_VERSION, **payload}, ensure_ascii=False,
                             sort_keys=True, default=lambda x: asdict(x) if hasattr(x, '__dataclass_fields__') else str(x))
        evidence_id = 'ev:v1:' + hashlib.sha256(encoded.encode()).hexdigest()
        result.append(LearningEvidence(evidence_id=evidence_id, **payload))

    for index, card in enumerate(review.phrase_cards):
        metadata = tuple(sorted((key, '' if value is None else str(value))
                                for key, value in card.model_dump().items() if key != 'phrase'))
        add(EvidenceType.PHRASE_CARD, 'phrase_cards', index, card.phrase,
            links=_links(card.phrase, 'value'), metadata=metadata)

    for index, item in enumerate(review.more_natural_expressions):
        add(EvidenceType.CORRECTION_RECORD, 'more_natural_expressions', index,
            item.your_phrase, corrected=item.more_natural, note=item.note,
            links=(_links(item.your_phrase, 'original') + _links(item.more_natural, 'corrected')
                   + tuple(ExpressionLink(link.expression_id, link.expression, 'note_quoted')
                           for link in _links(item.note, 'quoted', prose=True))),
            metadata=(('source_review_id', item.source_review_id),
                      ('source_review_date', str(item.source_review_date or ''))))

    fields = (
        ('expressions_to_add', EvidenceType.SUGGESTED_EXPRESSION, False),
        ('expressions_to_use_next_time', EvidenceType.NEXT_PRACTICE_EXPRESSION, False),
        ('words_and_phrases_actually_used', EvidenceType.EXPLICIT_USAGE_RECORD, False),
        ('weak_points', EvidenceType.WEAKNESS_RECORD, True),
        ('good_points', EvidenceType.POSITIVE_FEEDBACK, True),
        ('my_draft', EvidenceType.DRAFT_RECORD, False),
        ('more_natural_version', EvidenceType.CORRECTED_VERSION_RECORD, False),
        ('why_it_was_corrected', EvidenceType.CORRECTION_EXPLANATION, True),
    )
    for field, kind, prose in fields:
        for index, text in enumerate(getattr(review, field)):
            add(kind, field, index, text, links=_links(text, 'value', prose=prose))
    for field in ('situation', 'comment'):
        value = getattr(review, field)
        if value:
            # Context is retained, but not promoted to a learned English expression.
            add(EvidenceType.CONTEXT_RECORD, field, 0, value)
    return tuple(result)
