#!/usr/bin/env python3
"""Verify repeat migration, saved-data display and snapshot sync without network access."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src import data_loader
from src.analytics import summarize_reviews
from src.models import Review,FetchState
from src.migration_workspace import WorkspaceStorage,file_inventory,verify_protected
from src.notion_client import NotionReviewBlock
from src.review_identity import sync_review_blocks
from src.review_migration import validate_restored_reviews
from scripts.rehearse_review_migration import run_rehearsal


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-root',type=Path,required=True)
    args=parser.parse_args();root=args.work_root
    storage=WorkspaceStorage(root)
    if not storage.exists('reports/migration_first.json'):
        storage.save_json('reports/migration_first.json',storage.load_json('migration_result.json'))
    canonical=run_rehearsal(root,True)
    def components():
        return {prefix+'/'+path:entry for prefix in ['raw','processed','state','chroma'] for path,entry in file_inventory(root/prefix).items()}
    before=components()
    repeat=run_rehearsal(root,True)
    after=components()
    if before != after:raise ValueError('Repeat migration changed persisted data/index components')
    if repeat['counts'].get('added',0) or repeat['counts'].get('excluded',0):raise ValueError('Repeat migration is not idempotent')
    reviews=[Review.model_validate(r) for r in storage.load_json('processed/reviews.json')]
    state=FetchState.model_validate(storage.load_json('state/state.json'))
    metadata=storage.load_json('.migration_workspace.json')
    original_state=FetchState.model_validate_json((Path(metadata['backup'])/'state/state.json').read_text())
    if state.reviews != original_state.reviews:
        raise ValueError('Historical per-review incremental state was changed')
    validate_restored_reviews(reviews,state)
    with patch.object(data_loader,'get_storage',return_value=storage),patch.object(data_loader,'NotionClient') as client:
        display=data_loader.load_or_fetch_reviews()
        client.assert_not_called()
    if {r.review_id for r in display.reviews}!={r.review_id for r in reviews}:raise ValueError('Saved-data display differs')
    summary=summarize_reviews(reviews,'all')
    if summary.review_count != len(reviews) or summary.total_duration_minutes != sum(r.duration_minutes for r in reviews):
        raise ValueError('Analytics disagrees with migrated data')
    memory_state=state.model_copy(deep=True);memory_reviews=reviews;counts=Counter()
    for page in storage.load_json('notion_latest.json')['pages']:
        blocks=tuple(NotionReviewBlock(r['source_block_id'],r['markdown']) for r in page['reviews'])
        result=sync_review_blocks(page['page_id'],page['title'],blocks,memory_reviews,memory_state,[])
        counts.update({'added':result.added_count,'updated':result.updated_count,'skipped':result.skipped_count,'held':len(result.held)})
        memory_reviews=result.reviews
    if counts['added'] or counts['updated'] or {r.review_id for r in memory_reviews}!={r.review_id for r in reviews}:
        raise ValueError('Snapshot resync changed migrated reviews')
    report={'repeat_added':repeat['counts'].get('added',0),'repeat_excluded':repeat['counts'].get('excluded',0),
            'component_hashes_identical':True,'snapshot_sync':dict(counts),'display_count':len(display.reviews),
            'legacy_history_preserved':True,
            'summary':summary.model_dump(mode='json'),'state_review_count':len(state.current_reviews),
            'source_binding_count':len(state.source_bindings),'excluded_source_count':len(state.excluded_sources),
            'pending_count':len(state.pending_sources),'protected':verify_protected(root)}
    storage.save_json('data_verification.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
