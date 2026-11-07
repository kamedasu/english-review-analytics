from __future__ import annotations

from datetime import date
import hashlib
import json
import re
import unicodedata

from pydantic import BaseModel, ConfigDict
from src.models import Review

# Change these interests here; no Notion template or user profile changes are needed.
THEMES = (
    ('ヨガ・ピラティス', ('yoga', 'pilates', 'ヨガ', 'ピラティス')),
    ('ハイキング・自然', ('hiking', 'nature', 'ハイキング', '自然')),
    ('ランニング', ('running', 'ランニング')),
    ('カフェ・コーヒー', ('cafe', 'coffee', 'カフェ', 'コーヒー')),
    ('国際交流・友人との会話', ('friends', 'international', '友人', '国際交流')),
    ('気になる相手との日常会話・自然な誘い方', ('invite', 'invitation', '誘い')),
)
EXTRACTOR_VERSION = '1'


class Source(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    review_id: str
    date: date
    topic: str


class Candidate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    candidate_id: str
    expression: str
    meanings: list[str]
    sources: list[Source]
    fields: list[str]
    occurrence_count: int
    weak_review_count: int
    first_seen: date
    last_seen: date
    last_recorded_use: date | None
    days_since_seen: int
    theme_matches: int


def normalize(value: str) -> str:
    value = unicodedata.normalize('NFKC', value).casefold()
    value = value.translate(str.maketrans({'’': "'", '‘': "'", '“': '"', '”': '"', '–': '-', '—': '-'}))
    return re.sub(r'\s+', ' ', value).strip().strip('"\'`.,!?;:()[]{} ')


def contains(expression: str, text: str) -> bool:
    return re.search(r'(?<!\w)' + re.escape(normalize(expression)) + r'(?!\w)', normalize(text)) is not None


def expressions(value: str) -> list[str]:
    """Use explicit English fields or quoted English; don't turn Japanese advice into phrases."""
    quoted = re.findall(r'["“「`]([^"”」`\n]+)["”」`]', value)
    values = quoted or [value]
    return [v.strip() for v in values if re.search('[a-zA-Z]', v)
            and not re.search('[\u3040-\u30ff\u3400-\u9fff]', v)
            and 1 <= len(v.strip()) <= 160 and len(v.split()) <= 24]


def data_version(reviews: list[Review]) -> str:
    payload = [r.model_dump(mode='json', exclude={'raw_markdown'}) for r in sorted(reviews, key=lambda r: r.review_id)]
    encoded = json.dumps({'extractor': EXTRACTOR_VERSION, 'reviews': payload}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def extract_candidates(reviews: list[Review], *, limit: int = 10) -> list[Candidate]:
    if not reviews or limit <= 0:
        return []
    if len({r.review_id for r in reviews}) != len(reviews):
        raise ValueError('Duplicate review IDs')
    latest = max(r.date for r in reviews)
    grouped = {}
    for review in sorted(reviews, key=lambda r: (r.date, r.review_id)):
        fields = [('phrase_card', c.phrase, c.meaning) for c in review.phrase_cards]
        fields += [('more_natural', e.more_natural, '') for e in review.more_natural_expressions]
        fields += [(name, value, '') for name in ('expressions_to_add', 'expressions_to_use_next_time', 'weak_points')
                   for value in getattr(review, name)]
        for field, value, meaning in fields:
            for phrase in expressions(value):
                key = normalize(phrase)
                if not key:
                    continue
                item = grouped.setdefault(key, {'expression': phrase, 'sources': {}, 'fields': set(), 'meanings': set()})
                item['sources'][review.review_id] = Source(review_id=review.review_id, date=review.date, topic=review.topic)
                item['fields'].add(field)
                if meaning:
                    item['meanings'].add(meaning)
    # Normalize history once rather than once per candidate; retain field boundaries.
    history = []
    for review in reviews:
        learning = [c.phrase for c in review.phrase_cards] + [e.more_natural for e in review.more_natural_expressions]
        learning += review.expressions_to_add + review.expressions_to_use_next_time + review.weak_points
        usage = review.words_and_phrases_actually_used + review.my_draft
        history.append((review, [normalize(t) for t in learning + usage],
                        [normalize(t) for t in review.weak_points], [normalize(t) for t in usage]))
    candidates = []
    for key, item in grouped.items():
        seen, weak, used = [], set(), []
        pattern = re.compile(r'(?<!\w)' + re.escape(key) + r'(?!\w)')
        for review, recorded, weaknesses, usage in history:
            # Generated examples are never proof of actual usage.
            if any(pattern.search(text) for text in recorded):
                seen.append(review.date)
                item['sources'][review.review_id] = Source(review_id=review.review_id, date=review.date, topic=review.topic)
            if any(pattern.search(text) for text in weaknesses):
                weak.add(review.review_id)
            if any(pattern.search(text) for text in usage):
                used.append(review.date)
        sources = sorted(item['sources'].values(), key=lambda s: (s.date, s.review_id))
        last = max(seen or [s.date for s in sources])
        topics = ' '.join(s.topic for s in sources).casefold()
        themes = sum(any(word in topics or word in key for word in words) for _, words in THEMES)
        candidates.append(Candidate(
            candidate_id=hashlib.sha256(key.encode()).hexdigest()[:20], expression=item['expression'],
            meanings=sorted(item['meanings']), sources=sources, fields=sorted(item['fields']),
            occurrence_count=len(sources), weak_review_count=len(weak), first_seen=min(s.date for s in sources),
            last_seen=last, last_recorded_use=max(used) if used else None,
            days_since_seen=(latest-last).days, theme_matches=themes,
        ))
    return sorted(candidates, key=lambda c: (
        -(c.weak_review_count >= 2), -(c.days_since_seen >= 30), -c.weak_review_count,
        -c.days_since_seen, -c.theme_matches, c.candidate_id,
    ))[:limit]


def evidence(candidate: Candidate) -> str:
    usage = f'本人の発話・使用欄での最終記録: {candidate.last_recorded_use}' if candidate.last_recorded_use else '本人の発話・使用欄での再使用記録は確認できません'
    return (f'記録のあるレビュー {candidate.occurrence_count}件／弱点との関連 {candidate.weak_review_count}件。'
            f'最終登場 {candidate.last_seen}（最新レビューから{candidate.days_since_seen}日）。{usage}。'
            '記録上の傾向であり、実際に使えていないという判定ではありません。')
