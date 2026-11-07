#!/usr/bin/env python3
"""Incrementally update and verify only an explicitly marked workspace's Chroma copy."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ['ANONYMIZED_TELEMETRY'] = 'False'

from src.config import get_settings
from src.models import Review, FetchState
from src.migration_workspace import WorkspaceStorage,verify_protected
from src.review_migration import validate_restored_reviews
from src.rag.chroma_store import ChromaStore
from src.rag.document_builder import build_rag_documents
from src.rag.indexer import compute_rag_index_diff,update_rag_index_incrementally
from src.rag.embeddings import OpenAIEmbeddingProvider
from src.rag.retriever import RagRetriever


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-root',type=Path,required=True)
    parser.add_argument('--offline-check',action='store_true',help='Reopen persisted vectors and search without embedding/network calls')
    args = parser.parse_args()
    root = args.work_root.resolve()
    storage = WorkspaceStorage(args.work_root)
    verify_protected(root)
    reviews = [Review.model_validate(x) for x in storage.load_json('processed/reviews.json')]
    state = FetchState.model_validate(storage.load_json('state/state.json'))
    validate_restored_reviews(reviews,state)
    settings = get_settings()
    store = ChromaStore(root/'chroma',settings.rag_collection_name)
    if not store.collection_exists(): raise ValueError('Existing copied RAG collection is required; rebuild refused')
    before = store.get_all_documents()
    documents = build_rag_documents(reviews)
    diff = compute_rag_index_diff(documents,before)
    if args.offline_check:
        if diff.added or diff.changed or diff.deleted_ids:
            raise ValueError('Reopened RAG differs from processed')
        import numpy as np
        collection = store._get_collection()
        payload = collection.get(include=['embeddings'])
        vectors = np.asarray(payload['embeddings'])
        if vectors.ndim != 2 or len(vectors) != len(documents) or not np.isfinite(vectors).all():
            raise ValueError('Reopened embedding vectors are invalid')
        document_id = payload['ids'][0]
        by_id = {d.id:d for d in documents}
        hits = store.query(list(vectors[0]),k=5,where={'review_id':str(by_id[document_id].metadata['review_id'])})
        if document_id not in hits['ids'][0]: raise ValueError('Reopened vector index search failed')
        report = {'offline':True,'documents':len(before),'field_differences':0,'vector_shape':list(vectors.shape),
                  'persisted_search':True,'protected':verify_protected(root)}
        storage.save_json('rag_reopen_result.json',report)
        print(json.dumps(report,indent=2))
        return
    print(json.dumps({'planned_added':len(diff.added),'planned_changed':len(diff.changed),'planned_deleted':len(diff.deleted_ids)}),flush=True)
    provider = OpenAIEmbeddingProvider.from_settings()
    result = update_rag_index_incrementally(reviews,provider,store)
    after = store.get_all_documents()
    remaining = compute_rag_index_diff(documents,after)
    if remaining.added or remaining.changed or remaining.deleted_ids: raise ValueError('RAG contents differ from processed')
    import numpy as np
    collection = store._get_collection()
    vectors = np.asarray(collection.get(include=['embeddings'])['embeddings'])
    if vectors.shape[0] != len(documents) or vectors.ndim != 2 or not np.isfinite(vectors).all():
        raise ValueError('Invalid persisted embedding vectors')
    # Query with an actual persisted vector; no artificial embeddings or private text output.
    candidate_ids = [r.id for r in diff.added][:3] or [d.id for d in documents][:3]
    checks = []
    by_id = {d.id:d for d in documents}
    for document_id in candidate_ids:
        vector = collection.get(ids=[document_id],include=['embeddings'])['embeddings'][0]
        payload = store.query(list(vector),k=5,where={'review_id':str(by_id[document_id].metadata['review_id'])})
        matched = document_id in payload['ids'][0]
        checks.append(matched)
        if not matched: raise ValueError('Persisted vector search failed')
    retrieved = RagRetriever(provider,store).retrieve('English speaking practice',k=3)
    if not retrieved or any(hit.document.id not in by_id for hit in retrieved):
        raise ValueError('Query embedding/retriever integration failed')
    repeat = update_rag_index_incrementally(reviews,provider,store)
    if repeat.embedded_count or repeat.added_count or repeat.changed_count or repeat.deleted_count:
        raise ValueError('RAG repeat update is not idempotent')
    conn = sqlite3.connect((root/'chroma/chroma.sqlite3').as_uri()+'?mode=ro',uri=True)
    integrity = conn.execute('pragma quick_check').fetchall();conn.close()
    if integrity != [('ok',)]: raise ValueError('Workspace SQLite integrity failed')
    report = {'incremental':asdict(result),'before_documents':len(before),'after_documents':len(after),
              'field_differences':0,'vector_shape':list(vectors.shape),'vectors_finite':True,'search_checks':checks,
              'semantic_search_results':len(retrieved),
              'repeat_embedded_count':repeat.embedded_count,'sqlite':'ok','protected':verify_protected(root)}
    storage.save_json('rag_result.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Provider errors may contain API response text; never echo credentials or inputs.
        print('Workspace RAG verification failed: '+type(exc).__name__,file=sys.stderr)
        raise SystemExit(1)
