#!/usr/bin/env python3
"""Apply historical review restoration only inside a verified migration workspace."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data_loader import _save_sync
from src.models import Review, FetchState
from src.migration_workspace import WorkspaceStorage, verify_protected, require_workspace
from src.review_migration import prepare_restoration, validate_restored_reviews
from src.utils.hashing import content_hash


def run_rehearsal(root: Path, apply: bool = False) -> dict:
    metadata = require_workspace(root)
    storage = WorkspaceStorage(root)
    verify_protected(root)
    original = [Review.model_validate(x) for x in json.loads((Path(metadata['backup'])/'processed/reviews.json').read_text())]
    current = [Review.model_validate(x) for x in storage.load_json('processed/reviews.json')]
    state = FetchState.model_validate(storage.load_json('state/state.json'))
    baseline = storage.load_json('notion_20261103.json')
    latest = storage.load_json('notion_latest.json')
    result = prepare_restoration(original,current,state,baseline,latest)
    reviews = result['reviews']
    preserved = 0
    for old in original:
        matches = [r for r in reviews if r.review_id == old.review_id]
        if matches:
            before = old.model_dump(exclude={'source_block_id'})
            after = matches[0].model_dump(exclude={'source_block_id'})
            if before != after: raise ValueError('Existing review contents or nested references changed')
            preserved += 1
    report = {k:v for k,v in result.items() if k not in ['reviews','state','excluded']}
    report.update({
        'work_root':str(root.resolve()), 'applied':apply,
        'before_count':len(current),'after_count':len(reviews),'minutes':sum(r.duration_minutes for r in reviews),
        'preserved_original_ids':preserved,
        'months':{m:{'count':sum(r.date.strftime('%Y-%m')==m for r in reviews),'minutes':sum(r.duration_minutes for r in reviews if r.date.strftime('%Y-%m')==m)} for m in sorted({r.date.strftime('%Y-%m') for r in reviews})},
        'baseline_snapshot_sha256':content_hash(storage.load_text('notion_20261103.json')),
        'latest_snapshot_sha256':content_hash(storage.load_text('notion_latest.json')),
        'exclusion_reason':'empty_heading_2026-10-25; raw and Notion retained',
    })
    if not apply: return report
    writes = []
    for page in latest['pages']:
        base = f"raw/{page['title']}_{page['page_id']}"
        previous = base+'.md'
        original_path = 'audit/original_'+previous
        if storage.exists(previous) and not storage.exists(original_path):
            writes.append((original_path, storage.load_text(previous)))
        writes.append((base+'.md', page['markdown']))
        writes.append((base+'.identity.json', json.dumps({
            'identity_version':2,'page_id':page['page_id'],'page_hash':content_hash(page['markdown']),
            'reviews':[{k:r[k] for k in ('source_block_id','content_hash')} for r in page['reviews']],
            'held':[r for r in result['held'] if r['source_block_id'] in {e['source_block_id'] for e in page['reviews']}],
        },ensure_ascii=False,indent=2)))
    if result['excluded']:
        ledger = storage.load_json('audit/excluded_reviews.json') if storage.exists('audit/excluded_reviews.json') else []
        known = {r['review_id'] for r in ledger}
        ledger += [r|{'exclusion_reason':report['exclusion_reason']} for r in result['excluded'] if r['review_id'] not in known]
        writes.append(('audit/excluded_reviews.json',json.dumps(ledger,ensure_ascii=False,indent=2)))
    writes += [
        ('processed/reviews.json',json.dumps([r.model_dump(mode='json',exclude={'source_block_id'} if not r.source_block_id else set()) for r in reviews],ensure_ascii=False,indent=2)),
        ('migration_result.json',json.dumps(report,ensure_ascii=False,indent=2)),
        ('state/state.json',result['state'].model_dump_json(indent=2)),
    ]
    verify_protected(root)
    _save_sync(storage,writes)
    persisted = [Review.model_validate(x) for x in storage.load_json('processed/reviews.json')]
    validate_restored_reviews(persisted,FetchState.model_validate(storage.load_json('state/state.json')))
    report['protected'] = verify_protected(root)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-root',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    args = parser.parse_args()
    print(json.dumps(run_rehearsal(args.work_root,args.apply),ensure_ascii=False,indent=2))


if __name__ == '__main__': main()
