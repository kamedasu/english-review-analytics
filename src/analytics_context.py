"""Bounded Analytics views of the shared catalog; facts, never mastery inference."""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import asdict
from datetime import date, timedelta
import hashlib
import json

from src.analytics import summarize_reviews
from src.learning import build_catalog
from src.learning.models import EvidenceType as T
from src.learning.normalization import normalize_expression
from src.learning.profile import LEARNER_PROFILE

CONTEXT_VERSION = '1'
MAX_CONTEXT_BYTES = 32000
MAX_EVIDENCE = 28


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def period_bounds(label: str, period_type: str) -> tuple[date, date, date, date]:
    """Calendar periods, including year rollover and leap years."""
    if period_type == 'Monthly':
        year, month = map(int, label.split('-')); span = 1
    elif period_type == 'Quarterly':
        year_text, quarter_text = label.split('-Q'); year = int(year_text); quarter = int(quarter_text)
        if quarter not in range(1, 5):
            raise ValueError('Invalid quarter')
        month = (quarter-1)*3+1; span = 3
    elif period_type == 'Yearly':
        year, month, span = int(label), 1, 12
    else:
        raise ValueError('Invalid period type')
    start = date(year, month, 1)
    def shift(offset):
        index = year*12+month-1+offset
        return date(index//12, index%12+1, 1)
    return start, shift(span)-timedelta(days=1), shift(-span), start-timedelta(days=1)


def _scope(rows, start, end):
    return [r for r in rows if start <= r.date <= end]


def _metrics(rows, catalog, start, end, label):
    scoped = _scope(rows,start,end)
    summary = summarize_reviews(scoped,label)
    view = catalog.select(start_date=start,end_date=end)
    counts = Counter(r.evidence_type for r in view.evidence)
    expressions = view.all()
    return {
        'start': str(start), 'end': str(end), 'calendar_days': max(0,(end-start).days+1),
        'has_records': bool(scoped),
        'study_minutes': summary.total_duration_minutes, 'reviews': summary.review_count,
        'study_days': summary.study_days, 'longest_streak': summary.longest_streak,
        'phrase_card_records': summary.phrase_count,
        # Retain the existing count, rename only. Not unique expressions or actual use.
        'legacy_reappearance_matches': summary.reused_phrase_count,
        'unique_expressions':len(expressions),
        'reappearing_expressions':sum(e.statistics.unique_review_count >= 2 for e in expressions),
        'explicit_usage_records':counts[T.EXPLICIT_USAGE_RECORD],
        'correction_records':counts[T.CORRECTION_RECORD], 'weakness_records':counts[T.WEAKNESS_RECORD],
        'record_types':dict(sorted((str(k),v) for k,v in counts.items())),
    }


def comparison(current, previous):
    keys = ('study_minutes','reviews','study_days','explicit_usage_records','correction_records','weakness_records')
    return {key:{'delta':current[key]-previous[key] if previous['has_records'] else None,
                 'percent':round((current[key]-previous[key])/previous[key]*100,1)
                 if previous['has_records'] and previous[key] else None} for key in keys}


def build_analysis_context(reviews, label: str, period_type='Monthly', *, today: date | None=None):
    today = today or date.today()
    start, calendar_end, previous_start, previous_end = period_bounds(label,period_type)
    end = min(calendar_end,today)
    if start > end:
        raise ValueError('Future periods cannot be analysed')
    # One shared Foundation, reconstructed from the authoritative processed data.
    rows = [r for r in reviews if r.date <= end]
    catalog = build_catalog(rows)
    current = _metrics(rows,catalog,start,end,label)
    previous = _metrics(rows,catalog,previous_start,previous_end,'previous')
    context = {
        'context_version':CONTEXT_VERSION, 'period_type':period_type, 'period':label,
        'learner_profile':asdict(LEARNER_PROFILE),
        'facts':{'current':current,'previous':previous,'comparison':comparison(current,previous),
                 'complete_period':end==calendar_end,
                 'equal_observed_days':current['calendar_days']==previous['calendar_days'],
                 'comparison_status':'records_available' if previous['has_records'] else 'no_comparison_records'},
        'definitions':{
            'phrase_card_records':'Number of Phrase Card entries, including repeat entries.',
            'legacy_reappearance_matches':'Existing source-target to later-review substring matches; not unique phrases or actual use.',
            'reappearing_expressions':'Exact normalized catalog expressions linked to at least two distinct review IDs in the period.',
            'explicit_usage_records':'Source entries explicitly labelled actually used, not proof of correctness.',
            'correction_records':'Recorded correction pairs, including stylistic suggestions; not all grammar errors.',
            'missing_usage':'No explicit usage record in this scoped history, not evidence of inability.',
        },
        'topics':[], 'evidence':[], 'sampling':{},
    }
    topics = defaultdict(Counter)
    for r in rows:
        scope = 'current' if r.date >= start else 'previous' if r.date >= previous_start else 'historical'
        if r.topic:
            topics[scope][r.topic[:100]] += 1
    context['topics'] = [{'scope':scope,'topic':topic,'reviews':count}
                         for scope in ('current','previous','historical')
                         for topic,count in topics[scope].most_common(5)]
    # Diversify by source, period, month and Topic. Alternate newest/oldest per
    # stratum; never collapse semantically similar issues using keyword rules.
    groups = defaultdict(list)
    signatures = defaultdict(list)
    for r in catalog.evidence:
        signature=(str(r.evidence_type),normalize_expression(r.original_text),
                   normalize_expression(r.corrected_text),normalize_expression(r.note))
        signatures[signature].append(r)
        scope='current' if r.review_date>=start else 'previous' if r.review_date>=previous_start else 'historical'
        key=(scope,str(r.evidence_type),str(r.review_date)[:7],normalize_expression(r.topic))
        groups[key].append(r)
    strata=defaultdict(lambda:defaultdict(deque))
    for key,records in sorted(groups.items(),key=lambda item:(item[0][:3], max(r.review_date for r in item[1]), item[0][3]),reverse=True):
        strata[(key[0],key[1])][key[2]].append(deque(sorted(records,key=lambda r:(r.review_date,r.evidence_id),reverse=True)))
    queues=defaultdict(deque)
    for key,months in strata.items():
        # Interleave months before returning to another Topic in the same month.
        # This prevents a busy recent month from occupying every excerpt slot.
        while any(months.values()):
            for month in sorted(months,reverse=True):
                if months[month]: queues[key].append(months[month].popleft())
    priorities=[T.WEAKNESS_RECORD,T.CORRECTION_RECORD,T.EXPLICIT_USAGE_RECORD,T.PHRASE_CARD,
                T.SUGGESTED_EXPRESSION,T.NEXT_PRACTICE_EXPRESSION,T.POSITIVE_FEEDBACK]
    keys=[(scope,str(kind)) for scope in ('current','previous','historical') for kind in priorities]
    seen=set(); available=len(catalog.evidence)
    while len(context['evidence'])<MAX_EVIDENCE and any(queues[k] for k in keys):
        for key in keys:
            queue=queues[key]
            if not queue: continue
            bucket=queue.popleft();record=bucket.popleft()
            # Return the remaining stratum at the end; oldest next, then newest.
            bucket.reverse()
            if bucket: queue.append(bucket)
            signature=(str(record.evidence_type),normalize_expression(record.original_text),
                       normalize_expression(record.corrected_text),normalize_expression(record.note))
            if signature in seen: continue
            seen.add(signature)
            matches=signatures[signature]
            links=set(record.expression_ids)
            last=max(r.review_date for r in matches)
            subsequent=[r for expression_id in links for r in catalog.history(expression_id)
                        if r.review_date>last]
            subsequent={r.evidence_id:r for r in subsequent}.values()
            item={
                'evidence_id':record.evidence_id,'review_id':record.review_id,'date':str(record.review_date),
                'scope':key[0],'type':str(record.evidence_type),'topic':record.topic[:100],
                'original':record.original_text[:260],'corrected':record.corrected_text[:260],'note':record.note[:320],
                'clipped':any(len(text)>limit for text,limit in [(record.original_text,260),(record.corrected_text,260),(record.note,320)]),
                'first_recorded':str(min(r.review_date for r in matches)), 'last_recorded':str(last),
                'current_records':sum(start<=r.review_date<=end for r in matches),
                'previous_records':sum(previous_start<=r.review_date<=previous_end for r in matches),
                'later_correction_dates':sorted({str(r.review_date) for r in subsequent if r.evidence_type==T.CORRECTION_RECORD})[-3:],
                'later_usage_dates':sorted({str(r.review_date) for r in subsequent if r.evidence_type==T.EXPLICIT_USAGE_RECORD})[-3:],
                'followup_basis':'Exact shared expression IDs only; not proof of resolved issue or correct use.',
                'period_explicit_usage_records':len({r.evidence_id for expression_id in links
                    for r in catalog.history(expression_id,start_date=start,end_date=end) if r.evidence_type==T.EXPLICIT_USAGE_RECORD}),
            }
            if len(encode({**context,'evidence':[*context['evidence'],item]}).encode()) <= MAX_CONTEXT_BYTES-1000:
                context['evidence'].append(item)
            if len(context['evidence'])>=MAX_EVIDENCE: break
    context['sampling']={'available_records':available,'selected_records':len(context['evidence']),
                         'max_records':MAX_EVIDENCE,'not_exhaustive':True}
    # Fingerprint all processed facts up to the effective end, not just excerpts.
    context['source_hash']=catalog.source_data_hash
    if len(encode(context).encode())>MAX_CONTEXT_BYTES:
        raise ValueError('Analytics context too large')
    return context


def context_fingerprint(context, model, prompt_version):
    return hashlib.sha256(encode([context,model,prompt_version]).encode()).hexdigest()
