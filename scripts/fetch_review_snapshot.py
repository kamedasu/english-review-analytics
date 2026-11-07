#!/usr/bin/env python3
"""Fetch state-derived monthly Notion pages with GET only into an isolated workspace."""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests
from src.config import get_settings
from src.models import FetchState
from src.migration_workspace import WorkspaceStorage, verify_protected
from src.notion_client import NOTION_VERSION, blocks_to_review_markdown, _extract_title
from src.notion_parser import split_reviews
from src.utils.hashing import content_hash


def fetch_snapshot(targets: dict[str, str], api_key: str, get=None, interval: float = .36) -> dict:
    get = get or requests.get
    lock = threading.Lock()
    next_request = 0.
    request_count = 0
    status_counts = Counter()

    def read(path, params=None):
        nonlocal next_request, request_count
        for attempt in range(3):
            with lock:
                now = time.monotonic()
                delay = max(0, next_request-now)
                next_request = max(now, next_request)+interval
            if delay: time.sleep(delay)
            try:
                response = get('https://api.notion.com/v1'+path, headers={
                    'Authorization': 'Bearer '+api_key, 'Notion-Version': NOTION_VERSION,
                }, params=params, timeout=(10, 30))
            except requests.RequestException:
                raise RuntimeError('Notion network request failed; no fallback snapshot is permitted') from None
            with lock:
                request_count += 1
                status_counts[response.status_code] += 1
            if response.status_code == 429 and attempt < 2:
                time.sleep(min(float(response.headers.get('Retry-After', '1')), 30))
                continue
            if response.status_code != 200:
                raise RuntimeError(f'Notion GET failed: HTTP {response.status_code}')
            return response.json()
        raise RuntimeError('Notion rate limit retries exhausted')

    def fetch(page_id, expected_title):
        page = read('/pages/'+page_id)
        title = _extract_title(page)
        if not re.fullmatch(r'[0-9]{4}-[0-9]{2}', title): raise ValueError('Invalid monthly page title')
        date(int(title[:4]), int(title[5:]), 1)
        if title != expected_title or page.get('archived') or page.get('in_trash'): raise ValueError('Page title/status changed')
        stats = Counter()
        visited = set()
        def children(block_id):
            if block_id in visited: raise ValueError('Repeated node in Notion block tree')
            visited.add(block_id)
            blocks = []
            cursor = None
            cursors = set()
            while True:
                params = {'page_size':100}
                if cursor: params['start_cursor'] = cursor
                payload = read('/blocks/'+block_id+'/children', params)
                stats['list_requests'] += 1
                stats['cursor_requests'] += bool(cursor)
                for block in payload.get('results', []):
                    stats['blocks'] += 1
                    if block.get('has_children'):
                        if block['type'] in {'child_page', 'child_database'}:
                            raise ValueError('Nested page/database requires separate authorization')
                        block['children'] = children(block['id'])
                    blocks.append(block)
                if not payload.get('has_more'): break
                cursor = payload.get('next_cursor')
                if not cursor or cursor in cursors: raise ValueError('Invalid pagination cursor')
                cursors.add(cursor)
            return blocks
        blocks = children(page_id)
        end = read('/pages/'+page_id)
        if page.get('last_edited_time') != end.get('last_edited_time'): raise ValueError('Page changed during snapshot read')
        markdown, reviews = blocks_to_review_markdown(blocks)
        ids = [b.source_block_id for b in reviews]
        if not all(ids) or len(ids) != len(set(ids)) or [b.markdown for b in reviews] != split_reviews(markdown):
            raise ValueError('Untrustworthy heading-to-Markdown mapping')
        result = {'page_id':page_id, 'title':title, 'last_edited_time':page.get('last_edited_time'), 'markdown':markdown,
                  'reviews':[{'source_block_id':b.source_block_id,'markdown':b.markdown,'content_hash':content_hash(b.markdown)} for b in reviews], 'stats':dict(stats)}
        print(json.dumps({'month':title,'headings':len(reviews),'stats':dict(stats)}), flush=True)
        return result

    results = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(fetch, pid, title) for pid,title in targets.items()]
        for future in as_completed(futures): results.append(future.result())
    all_ids = [r['source_block_id'] for p in results for r in p['reviews']]
    if len(all_ids) != len(set(all_ids)): raise ValueError('Heading source ID reused across pages')
    return {'schema_version':1,'captured_at':datetime.now(timezone.utc).isoformat(),'pages':sorted(results,key=lambda p:p['title']),
            'request_count':request_count,'http_status':dict(status_counts)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-root', type=Path, required=True)
    args = parser.parse_args()
    storage = WorkspaceStorage(args.work_root)
    verify_protected(args.work_root)
    if storage.exists('notion_latest.json'): raise ValueError('Refusing to overwrite an existing latest snapshot')
    state = FetchState.model_validate(storage.load_json('state/state.json'))
    targets = {pid:p.title for pid,p in state.pages.items() if p.title in {f'2026-{m:02d}' for m in range(4,12)}}
    if len(targets) != 8: raise ValueError('Expected exactly eight state-derived target pages')
    key = get_settings().notion_api_key
    if not key: raise ValueError('Notion API key is not configured')
    snapshot = fetch_snapshot(targets, key)
    verify_protected(args.work_root)
    storage.save_json('notion_latest.json', snapshot)
    print(json.dumps({'complete':True,'pages':len(snapshot['pages']),'headings':sum(len(p['reviews']) for p in snapshot['pages']),'requests':snapshot['request_count']}))


if __name__ == '__main__': main()
