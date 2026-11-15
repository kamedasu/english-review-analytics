from __future__ import annotations

from datetime import date
from dataclasses import asdict
from difflib import SequenceMatcher
import hashlib
import json
import re
import unicodedata

from pydantic import BaseModel, ConfigDict, Field
from src.models import Review
from src.coach.profile import (LEARNER_PROFILE, NUANCE_TERMS, GRAMMAR_TERMS,
                               PRACTICE_FAMILIES, suitability, application_value)

# Change these interests here; no Notion template or user profile changes are needed.
THEMES = (
    ('ヨガ・ピラティス', ('yoga', 'pilates', 'ヨガ', 'ピラティス')),
    ('ハイキング・自然', ('hiking', 'nature', 'ハイキング', '自然')),
    ('ランニング', ('running', 'ランニング')),
    ('カフェ・コーヒー', ('cafe', 'coffee', 'カフェ', 'コーヒー')),
    ('国際交流・友人との会話', ('friends', 'international', '友人', '国際交流')),
    ('気になる相手との日常会話・自然な誘い方', ('invite', 'invitation', '誘い')),
)
EXTRACTOR_VERSION = '3'


class Source(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    review_id: str
    date: date
    topic: str


class PracticeEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source: Source
    kind: str
    detail: str


class RelatedTarget(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expression: str
    sources: list[Source]
    practice_evidence: list[PracticeEvidence]


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
    correction_review_count: int = 0
    nuance_review_count: int = 0
    level_fit: str = 'legacy_unspecified'
    application_value: int = 0
    practice_evidence: list[PracticeEvidence] = Field(default_factory=list)
    related_targets: list[RelatedTarget] = Field(default_factory=list)
    selection_factors: list[str] = Field(default_factory=list)


def normalize(value: str) -> str:
    value = unicodedata.normalize('NFKC', value).casefold()
    value = value.translate(str.maketrans({'’': "'", '‘': "'", '“': '"', '”': '"', '–': '-', '—': '-'}))
    return re.sub(r'\s+', ' ', value).strip().strip('"\'`.,!?;:()[]{} ')


def contains(expression: str, text: str) -> bool:
    return re.search(r'(?<![a-z0-9_])' + re.escape(normalize(expression)) + r'(?![a-z0-9_])', normalize(text)) is not None


def quoted_expressions(value: str) -> list[str]:
    return (re.findall(r'["“「`]([^"”」`\n]+)["”」`]', value)
            + re.findall(r"(?<![a-zA-Z])'([^'\n]+)'(?![a-zA-Z])", value))


def target_focus(key: str, text: str, quoted: set[str]) -> bool:
    return (len(key.split()) > 1 or key in quoted or text.startswith(key + ':')
            or bool(re.search(re.escape(key) + r'(?:の|を|は|と)', text)))


def expressions(value: str) -> list[str]:
    """Use explicit English fields or quoted English; don't turn Japanese advice into phrases."""
    quoted = quoted_expressions(value)
    contrasts = re.findall(r"[A-Za-z][A-Za-z'-]*(?: [A-Za-z][A-Za-z'-]*){0,3}(?:\s*/\s*[A-Za-z][A-Za-z'-]*(?: [A-Za-z][A-Za-z'-]*){0,3})+", value)
    values = quoted or [part.strip() for span in contrasts for part in span.split('/')] or [value]
    return [v.strip() for v in values if re.search('[a-zA-Z]', v)
            and not re.search('[\u3040-\u30ff\u3400-\u9fff]', v)
            and 1 <= len(v.strip()) <= 160 and len(v.split()) <= 24]


def data_version(reviews: list[Review]) -> str:
    payload = [r.model_dump(mode='json', exclude={'raw_markdown'}) for r in sorted(reviews, key=lambda r: r.review_id)]
    encoded = json.dumps({'extractor': EXTRACTOR_VERSION, 'profile': asdict(LEARNER_PROFILE), 'themes': THEMES, 'reviews': payload}, ensure_ascii=False, sort_keys=True)
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
        fields += [('usage_note', e.note, '') for e in review.more_natural_expressions if NUANCE_TERMS.search(e.note)]
        fields += [(name, value, '') for name in ('expressions_to_add', 'expressions_to_use_next_time', 'weak_points')
                   for value in getattr(review, name)]
        for field, value, meaning in fields:
            for phrase in expressions(value):
                if field == 'weak_points' and not re.search(r'["“「`]', value):
                    if len(phrase.split()) > 4 or re.match(r'(?i)^(use|practice|remember|avoid|try)\b', phrase):
                        continue
                key = normalize(phrase)
                if not key:
                    continue
                item = grouped.setdefault(key, {'expression': phrase, 'sources': {}, 'fields': set(), 'meanings': set()})
                item['sources'][review.review_id] = Source(review_id=review.review_id, date=review.date, topic=review.topic)
                item['fields'].add(field)
                if meaning:
                    item['meanings'].add(meaning)
    # Normalize and diff each correction once; a target merely appearing in the
    # unchanged part of a corrected sentence is not itself evidence of a mistake.
    history = []
    for review in reviews:
        learning = [c.phrase for c in review.phrase_cards]
        learning += [text for e in review.more_natural_expressions for text in (e.your_phrase, e.more_natural, e.note)]
        learning += review.expressions_to_add + review.expressions_to_use_next_time + review.weak_points
        usage = review.words_and_phrases_actually_used + review.my_draft
        weak_records = [(text, normalize(text),
                         {normalize(p) for p in quoted_expressions(text)},
                         bool(NUANCE_TERMS.search(text))) for text in review.weak_points]
        corrections = []
        for e in review.more_natural_expressions:
            before, after = normalize(e.your_phrase), normalize(e.more_natural)
            old_words = re.findall(r"[a-z]+(?:['-][a-z]+)*", before)
            new_words = re.findall(r"[a-z]+(?:['-][a-z]+)*", after)
            changed = set()
            for tag, i, j, k, l in SequenceMatcher(None, old_words, new_words).get_opcodes():
                if tag != 'equal':
                    changed.update(old_words[i:j] + new_words[k:l])
            note = normalize(e.note)
            shared_grammar = any(term in note and any(term in w[1] for w in weak_records) for term in GRAMMAR_TERMS)
            corrections.append((e, before, after, changed, note, bool(NUANCE_TERMS.search(e.note)), shared_grammar,
                                {normalize(p) for p in quoted_expressions(e.note)}))
        history.append((review, [normalize(t) for t in learning + usage],
                        [normalize(t) for t in review.weak_points], [normalize(t) for t in usage], corrections, weak_records))
    candidates = []
    for key, item in grouped.items():
        seen, weak, used = [], set(), []
        corrected, nuanced, records = set(), set(), []
        pattern = re.compile(r'(?<![a-z0-9_])' + re.escape(key) + r'(?![a-z0-9_])')
        meaningful = set(re.findall(r"[a-z]+(?:['-][a-z]+)*", key)) - {'i', 'you', 'it', 'the', 'a', 'to', 'of', 'and'}
        for review, recorded, weaknesses, usage, corrections, weak_records in history:
            source = Source(review_id=review.review_id, date=review.date, topic=review.topic)
            if any(pattern.search(text) for text in recorded):
                seen.append(review.date)
                item['sources'][review.review_id] = source
            if any(pattern.search(text) for text in weaknesses):
                weak.add(review.review_id)
            if any(pattern.search(text) for text in usage):
                used.append(review.date)
            for text, normalized, quoted, has_nuance in weak_records:
                focused = target_focus(key, normalized, quoted)
                if focused and has_nuance and pattern.search(normalized):
                    nuanced.add(review.review_id)
                    records.append(PracticeEvidence(source=source, kind='weak_point_detail', detail=text[:240]))
            for e, before, after, changed, note, has_nuance, shared_grammar, note_quoted in corrections:
                linked = bool(pattern.search(before) or pattern.search(after))
                target_changed = bool(e.your_phrase and e.more_natural and before != after
                                      and linked and meaningful & changed)
                note_linked = has_nuance and ((bool(pattern.search(note)) and target_focus(key, note, note_quoted)) or target_changed)
                if target_changed:
                    corrected.add(review.review_id)
                    records.append(PracticeEvidence(source=source, kind='correction_pair',
                        detail=f'修正前: {e.your_phrase[:90]} / 言い換え: {e.more_natural[:90]}'))
                    # A specific grammar label shared by a note and a Weak point
                    # links the practice target without inventing an English phrase.
                    if shared_grammar:
                        weak.add(review.review_id)
                if note_linked:
                    nuanced.add(review.review_id)
                    records.append(PracticeEvidence(source=source, kind='usage_note', detail=e.note[:240]))
                if target_changed or note_linked:
                    item['sources'][review.review_id] = source
        fit = suitability(key, item['fields'], bool(corrected or nuanced))
        if fit == 'insufficient':
            continue  # Never fill remaining slots with unsubstantiated basic vocabulary.
        sources = sorted(item['sources'].values(), key=lambda s: (s.date, s.review_id))
        last = max(seen or [s.date for s in sources])
        topics = ' '.join(s.topic for s in sources).casefold()
        themes = sum(any(word in topics or word in key for word in words) for _, words in THEMES)
        factors = [fit]
        if corrected: factors.append('recorded_correction')
        if nuanced: factors.append('recorded_usage_or_nuance_issue')
        if len(weak) >= 2: factors.append('repeated_weak_point')
        if (latest-last).days >= 30: factors.append('not_recently_recorded')
        candidates.append(Candidate(
            candidate_id=hashlib.sha256(key.encode()).hexdigest()[:20], expression=item['expression'],
            meanings=sorted(item['meanings']), sources=sources, fields=sorted(item['fields']),
            occurrence_count=len(sources), weak_review_count=len(weak), first_seen=min(s.date for s in sources),
            last_seen=last, last_recorded_use=max(used) if used else None,
            days_since_seen=(latest-last).days, theme_matches=themes,
            correction_review_count=len(corrected), nuance_review_count=len(nuanced), level_fit=fit,
            practice_evidence=sorted(records, key=lambda e: (e.kind == 'correction_pair', -e.source.date.toordinal())),
            application_value=application_value(key), selection_factors=factors,
        ))
    ranked = sorted(candidates, key=lambda c: (
        -bool(c.correction_review_count or c.nuance_review_count),
        c.level_fit == 'foundation_with_evidence', -(c.weak_review_count >= 2), -c.application_value,
        -c.nuance_review_count, -c.correction_review_count, -c.weak_review_count,
        -(c.days_since_seen >= 30), -c.days_since_seen, -c.theme_matches, c.candidate_id,
    ))
    selected, seen_meanings = [], {}
    for candidate in ranked:
        key = normalize(candidate.expression)
        family = next((group for group in PRACTICE_FAMILIES if key in group), None)
        same_family = next((c for c in selected if family and normalize(c.expression) in family), None)
        # Only deduplicate identical short meanings without distinct challenge evidence.
        meaning = normalize(candidate.meanings[0]) if len(candidate.meanings) == 1 else ''
        same_meaning = seen_meanings.get(meaning) if len(meaning) >= 4 and not candidate.practice_evidence else None
        representative = same_family or same_meaning
        if representative is not None:
            representative.related_targets.append(RelatedTarget(expression=candidate.expression,
                sources=candidate.sources, practice_evidence=candidate.practice_evidence))
            continue
        selected.append(candidate)
        if meaning and not candidate.practice_evidence:
            seen_meanings[meaning] = candidate
    return selected[:limit]


def evidence(candidate: Candidate) -> str:
    usage = f'本人の発話・使用欄での最終記録: {candidate.last_recorded_use}' if candidate.last_recorded_use else '本人の発話・使用欄での再使用記録は確認できません'
    return (f'主表現について：修正・言い換えの対象 {candidate.correction_review_count}件／使い分け等の指摘 {candidate.nuance_review_count}件。'
            f'記録のあるレビュー {candidate.occurrence_count}件／弱点との関連 {candidate.weak_review_count}件。'
            f'最終登場 {candidate.last_seen}（最新レビューから{candidate.days_since_seen}日）。{usage}。'
            '記録上の傾向であり、実際に使えていないという判定ではありません。')


def choose_practice_candidates(pool: list[Candidate], *, limit: int = 10):
    """Disjoint 3/4/3 roles, using genuine issues and expressive targets only."""
    from src.coach.profile import is_foundation
    limit = max(0, min(limit, 10))
    selected, categories, used = [], {}, set()
    weakness = [c for c in pool if c.correction_review_count or c.nuance_review_count]
    expressive = [c for c in pool if c.application_value >= 2 and not is_foundation(normalize(c.expression))]
    expansion = sorted(expressive, key=lambda c: (-c.application_value, -c.theme_matches, -c.days_since_seen, c.candidate_id))
    practical = sorted(expressive, key=lambda c: (-c.theme_matches, -c.application_value, c.candidate_id))
    def take(candidates, category, count):
        added = 0
        for c in candidates:
            if len(selected) >= limit or added >= count:
                break
            if c.candidate_id in used:
                continue
            selected.append(c);used.add(c.candidate_id);categories[c.candidate_id] = category;added += 1
    take(weakness, 'weakness', 3)
    take(expansion, 'expression', 4)
    take(practical, 'conversation', 3)
    # Prefer expressive practice when a category is short; no duplicate or invented target.
    take(expansion, 'expression', limit-len(selected))
    take(weakness, 'weakness', limit-len(selected))
    return selected, categories
