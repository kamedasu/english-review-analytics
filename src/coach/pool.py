"""Coach-only bounded sampling of the shared catalog; no difficulty or mastery inference."""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import asdict, dataclass
import hashlib
import json
import re

from pydantic import Field
from src.models import Review
from src.learning import build_catalog, ExpressionCatalog
from src.learning.models import EvidenceType as T, LearningEvidence
from src.learning.normalization import normalize_expression
from src.learning.profile import LEARNER_PROFILE
from src.coach.candidates import Candidate, Source, data_version

POOL_VERSION = '6'
# Evidence/prompt revisions must not arbitrarily reshuffle the comparison pool.
SAMPLING_VERSION = '4'
MAX_POOL_CANDIDATES = 100
MAX_EXPRESSION_CHARS = 200  # Context-size limit, not a difficulty heuristic.
MAX_RECORDS = 4
TEXT_SNIPPET_CHARS = 240
NOTE_SNIPPET_CHARS = 320
MAX_CANDIDATE_BYTES = 5000
MAX_INPUT_BYTES = 180000
ORIGINS = (T.PHRASE_CARD, T.CORRECTION_RECORD, T.SUGGESTED_EXPRESSION,
           T.NEXT_PRACTICE_EXPRESSION, T.EXPLICIT_USAGE_RECORD, T.WEAKNESS_RECORD, T.POSITIVE_FEEDBACK)
# User report, not a blacklist or permanent competence assessment.
SELF_REPORTED_COMFORTABLE = ('enjoy', 'each other', "that's why", "I'm going to", 'I try to',
                             'one more time', 'One more time, please.', 'too much')


class PoolCandidate(Candidate):
    catalog_statistics: dict = Field(default_factory=dict)
    representative_records: list[dict] = Field(default_factory=list)
    recorded_examples: list[str] = Field(default_factory=list)
    sampled_origin: str = ''
    sampled_month: str = ''
    sampled_topic: str = ''


def _excerpt(text: str, targets: list[str], limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    matches = [match for target in targets if target
               for match in [re.search(re.escape(target), text, re.I)] if match]
    anchor = min((match.start() for match in matches), default=0)
    start = max(0, min(anchor-limit//3, len(text)-limit))
    return text[start:start+limit], True


def _snapshot(record: LearningEvidence, key: str) -> dict:
    targets = [link.expression for link in record.links if link.expression_id == key]
    snippets, clipped = {}, []
    for name, text, limit in [('original',record.original_text,TEXT_SNIPPET_CHARS),
                              ('corrected',record.corrected_text,TEXT_SNIPPET_CHARS),
                              ('note',record.note,NOTE_SNIPPET_CHARS)]:
        snippets[name], was_clipped = _excerpt(text, targets, limit)
        if was_clipped:
            clipped.append(name)
    value = {'evidence_id': record.evidence_id, 'review_id': record.review_id,
             'date': str(record.review_date), 'type': str(record.evidence_type),
             'source_field': record.source_field, 'source_index': record.source_index,
             'topic': record.topic[:60], **snippets,
             'roles': sorted({link.role for link in record.links if link.expression_id == key})}
    if record.evidence_type == T.CORRECTION_RECORD:
        # Structural facts only, never a grammatical/stylistic classification.
        if record.original_text.strip() and record.corrected_text.strip():
            status = 'both_sides_same' if record.original_text == record.corrected_text else 'both_sides_different'
        elif record.corrected_text.strip():
            status = 'corrected_only'
        elif record.original_text.strip():
            status = 'original_only'
        else:
            status = 'note_only'
        value['pair_status'] = status
    if clipped:
        value['clipped_fields'] = clipped
    return value


def representative_records(records: tuple[LearningEvidence, ...], key: str) -> list[dict]:
    corrections = [r for r in records if r.evidence_type == T.CORRECTION_RECORD]
    noted = [r for r in corrections if r.note.strip()]
    weaknesses = [r for r in records if r.evidence_type == T.WEAKNESS_RECORD]
    usages = [r for r in records if r.evidence_type == T.EXPLICIT_USAGE_RECORD]
    choices = []
    # Keep actual Weak Points even when a correction exists. Prefer a documented
    # explanation over another unexplained pair, and retain latest usage separately.
    priorities = ([corrections[-1]] if corrections else [])
    priorities += ([noted[0]] if noted else corrections[:1])
    priorities += weaknesses[-1:] + usages[-1:] + list(records[:1]) + list(records[-1:])
    for record in priorities:
        if record not in choices:
            choices.append(record)
    return [_snapshot(r, key) for r in choices[:MAX_RECORDS]]


def _eligible(record: LearningEvidence, key: str) -> bool:
    if record.evidence_type not in ORIGINS:
        return False
    # An erroneous original alone is not a recommended target. It stays in the
    # correction snapshot, paired with the recorded corrected form.
    return any(link.expression_id == key and link.role != 'original' for link in record.links)


def candidate_input(candidate: PoolCandidate) -> dict:
    return {'expression_id': candidate.candidate_id, 'expression': candidate.expression,
            'meanings': [m[:100] for m in candidate.meanings[:2]],
            'recorded_examples': candidate.recorded_examples,
            'statistics': candidate.catalog_statistics,
            'evidence_types': candidate.fields,
            'topics': sorted({source.topic[:60] for source in candidate.sources})[:2],
            # Source-field positions, review IDs and Topic remain in the internal
            # snapshot; evidence_id is enough for model citations and Python lookup.
            'records': [{k:v for k,v in r.items() if k not in
                         ('review_id','source_field','source_index','topic')}
                        for r in candidate.representative_records]}


def generation_input(candidates: list[PoolCandidate], practice_level: str, previous_questions: list[dict] | None = None) -> dict:
    return {'learner_profile': asdict(LEARNER_PROFILE), 'target_practice_cefr': 'B2-oriented',
            'practice_level': practice_level,
            'self_report': {'comfortable_basic_expressions': SELF_REPORTED_COMFORTABLE,
                            'interpretation': 'User report, not proof of permanent mastery. Specific challenges may justify selection.'},
            'previous_questions': previous_questions or [],
            'candidates': [candidate_input(c) for c in candidates]}


def serialize_input(candidates: list[PoolCandidate], practice_level: str, previous_questions: list[dict] | None = None) -> str:
    return json.dumps(generation_input(candidates, practice_level, previous_questions), ensure_ascii=False, separators=(',', ':'))


def plan_data_version(reviews: list[Review]) -> str:
    payload = [data_version(reviews), POOL_VERSION, asdict(LEARNER_PROFILE), SELF_REPORTED_COMFORTABLE,
               'generation-window-v1', MAX_POOL_CANDIDATES, MAX_INPUT_BYTES, MAX_CANDIDATE_BYTES, MAX_RECORDS, MAX_EXPRESSION_CHARS,
               TEXT_SNIPPET_CHARS, NOTE_SNIPPET_CHARS, SAMPLING_VERSION]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


@dataclass
class CandidatePool:
    catalog: ExpressionCatalog
    candidates: list[PoolCandidate]
    eligible_count: int


def prepare_candidate_pool(reviews: list[Review], *, limit: int = MAX_POOL_CANDIDATES, generation: int = 1) -> CandidatePool:
    if generation < 1:
        raise ValueError('Generation must be positive')
    catalog = build_catalog(reviews)  # The shared foundation is built exactly once.
    latest = max((r.date for r in reviews), default=None)
    expressions = {e.expression_id: e for e in catalog.all()
                   if len(e.expression) <= MAX_EXPRESSION_CHARS and '\n' not in e.expression}
    history = defaultdict(list)
    buckets = {kind: defaultdict(set) for kind in ORIGINS}
    for record in catalog.evidence:
        for key in record.expression_ids:
            if key not in expressions:
                continue
            history[key].append(record)
            if _eligible(record, key):
                bucket = (str(record.review_date)[:7], normalize_expression(record.topic))
                buckets[record.evidence_type][bucket].add(key)
    eligible_ids = {key for by_origin in buckets.values() for keys in by_origin.values() for key in keys}
    # Round-robin over source types, then (month, topic) strata. Hash ordering
    # avoids alphabetical prefixes or repetition counts becoming a quality score.
    queues = {}
    for kind, strata in buckets.items():
        by_month = defaultdict(deque)
        for bucket, keys in sorted(strata.items()):
            by_month[bucket[0]].append((bucket, keys))
        # Rotate the topic starting point by month, so all months do not begin
        # with the same alphabetically first topic in small bounded samples.
        for month_index, month in enumerate(sorted(by_month)):
            by_month[month].rotate(-month_index-(generation-1))
        ordered = []
        while any(by_month.values()):
            for month in sorted(by_month):
                if by_month[month]:
                    ordered.append(by_month[month].popleft())
        queues[kind] = deque((bucket, deque(sorted(keys, key=lambda key: hashlib.sha256(
            f'{SAMPLING_VERSION}:{kind}:{bucket}:{key}'.encode()).hexdigest())))
                             for bucket, keys in ordered)
    # Rotate within each stratum as well as across Topics. Keep the fixed hash
    # ordering, but advance its window for each successful generation.
    for queue in queues.values():
        for bucket, keys in queue:
            keys.rotate(-(generation-1))
    selected, used = [], set()
    limit = max(0, min(limit, MAX_POOL_CANDIDATES))
    while len(selected) < limit and any(queues.values()):
        for kind in ORIGINS:
            queue = queues[kind]
            picked = None
            while queue and picked is None:
                bucket, keys = queue.popleft()
                while keys and keys[0] in used:
                    keys.popleft()
                if keys:
                    picked = keys.popleft(), bucket
                if keys:
                    queue.append((bucket, keys))
            if picked is None:
                continue
            key, (month, topic) = picked
            used.add(key)
            expression = expressions[key]
            records = tuple(history[key]);stats = expression.statistics
            sources = {r.review_id: Source(review_id=r.review_id, date=r.review_date, topic=r.topic) for r in records}
            metadata = dict(stats.counts_by_type)
            value = PoolCandidate(
                candidate_id=key, expression=expression.expression, meanings=list(expression.meanings),
                sources=sorted(sources.values(), key=lambda s:(s.date,s.review_id)),
                fields=sorted(metadata), occurrence_count=stats.unique_review_count,
                weak_review_count=len({r.review_id for r in records if r.evidence_type == T.WEAKNESS_RECORD}),
                first_seen=stats.first_seen, last_seen=stats.last_seen, last_recorded_use=stats.last_usage_date,
                days_since_seen=(latest-stats.last_seen).days, theme_matches=0,
                correction_review_count=len({r.review_id for r in records if r.evidence_type == T.CORRECTION_RECORD}),
                level_fit='catalog_evidence_only',
                catalog_statistics={k: (str(v) if hasattr(v,'isoformat') else v)
                                    for k,v in asdict(stats).items() if k not in ('expression_id','counts_by_type')},
                representative_records=representative_records(records, key),
                recorded_examples=[example[:160] for example in expression.examples[:1]],
                sampled_origin=str(kind), sampled_month=month, sampled_topic=topic,
            )
            if len(json.dumps(candidate_input(value),ensure_ascii=False).encode()) > MAX_CANDIDATE_BYTES:
                continue
            if len(serialize_input([*selected,value], 'real_conversation').encode()) > MAX_INPUT_BYTES:
                continue
            selected.append(value)
            if len(selected) >= limit:
                break
    return CandidatePool(catalog, selected, len(eligible_ids))
