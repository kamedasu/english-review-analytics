"""Read-only inventory and in-memory preparation for reviewed identity migrations."""
from __future__ import annotations

from collections import Counter

from src.models import FetchState, Review
from src.notion_parser import parse_review, split_reviews
from src.review_identity import is_valid_learning_review
from src.storage.base import Storage
from src.utils.hashing import content_hash


def plan_identity_migration(storage: Storage) -> dict:
    reviews = [Review.model_validate(item) for item in storage.load_json("processed/reviews.json")]
    state = FetchState.model_validate(storage.load_json("state/state.json"))
    saved_hashes = {(r.source_page_id, r.content_hash) for r in reviews}
    months = []
    candidates = []
    invalid_saved = []
    for review in reviews:
        if not is_valid_learning_review(review):
            invalid_saved.append({"review_id": review.review_id, "date": review.date.isoformat(), "content_hash": review.content_hash})
    for page_id, page in sorted(state.pages.items(), key=lambda item: item[1].title):
        # Use the actual raw filename, without allowing a title to escape the storage root.
        paths = [path for path in storage.list_files("raw") if path.endswith(f"_{page_id}.md")]
        if len(paths) != 1:
            months.append({"month": page.title, "error": "missing_or_ambiguous_raw"})
            continue
        raw_path = paths[0]
        markdown = storage.load_text(raw_path)
        chunks = split_reviews(markdown)
        parsed = [(position, r) for position, chunk in enumerate(chunks, 1) if (r := parse_review(chunk, page_id, page.title)) is not None]
        source_counts = Counter(r.content_hash for _, r in parsed)
        saved_counts = Counter(r.content_hash for r in reviews if r.source_page_id == page_id)
        sidecar_path = raw_path[:-3] + ".identity.json"
        sidecar = storage.load_json(sidecar_path) if storage.exists(sidecar_path) else None
        valid_sidecar = bool(
            sidecar and sidecar.get("page_id") == page_id
            and sidecar.get("page_hash") == content_hash(markdown)
            and [entry.get("content_hash") for entry in sidecar.get("reviews", [])] == [content_hash(chunk) for chunk in chunks]
            and all(entry.get("source_block_id") for entry in sidecar.get("reviews", []))
            and len({entry["source_block_id"] for entry in sidecar.get("reviews", [])}) == len(chunks)
        )
        safe_matches = 0
        raw_only = 0
        for position, review in parsed:
            saved = (page_id, review.content_hash) in saved_hashes
            valid = is_valid_learning_review(review)
            if saved and valid and source_counts[review.content_hash] == saved_counts[review.content_hash] == 1:
                safe_matches += 1
            if not saved:
                raw_only += 1
                candidates.append({
                    "month": page.title, "date": review.date.isoformat(), "review_type": review.review_type,
                    "raw_position": position, "content_hash": review.content_hash,
                    "duration_minutes": review.duration_minutes,
                    "reason": "independence_confirmation_required" if valid else "empty_or_insufficient_review",
                })
        months.append({
            "month": page.title, "raw_count": len(parsed), "processed_count": sum(r.source_page_id == page_id for r in reviews),
            "raw_only_count": raw_only, "unique_valid_hash_matches": safe_matches,
            "source_mapping_available": valid_sidecar, "automatic_binding_ready": safe_matches if valid_sidecar else 0,
            "raw_state_hash_match": content_hash(markdown) == page.content_hash,
        })
    return {
        "mode": "read_only_plan", "processed_count": len(reviews),
        "duplicate_review_ids": sum(n - 1 for n in Counter(r.review_id for r in reviews).values() if n > 1),
        "raw_count": sum(m.get("raw_count", 0) for m in months),
        "raw_only_count": len(candidates), "identity_version": state.identity_version,
        "months": months, "raw_only_candidates": candidates, "invalid_saved_reviews": invalid_saved,
        "writes_performed": False,
    }


def prepare_approved_bindings(storage: Storage, decisions: dict) -> tuple[list[Review], FetchState]:
    """Validate a reviewed binding-only migration, returning an in-memory result.

    No raw-only restoration, deletion, fuzzy match, or network fetch is supported.
    Callers must explicitly commit the result separately, after review.
    """
    from src.storage.state_store import update_current_review_state

    for path in ("processed/reviews.json", "state/state.json"):
        if decisions.get("expected_sha256", {}).get(path) != content_hash(storage.load_text(path)):
            raise ValueError(f"Migration input changed or missing expected hash: {path}")
    reviews = [Review.model_validate(item) for item in storage.load_json("processed/reviews.json")]
    state = FetchState.model_validate(storage.load_json("state/state.json"))
    if state.identity_version > 2:
        raise ValueError("Unsupported identity state version")
    by_id = {r.review_id: r for r in reviews}
    if len(by_id) != len(reviews):
        raise ValueError("Duplicate saved review IDs")
    bindings = decisions.get("bindings", [])
    if not isinstance(bindings, list) or not bindings:
        raise ValueError("Reviewed bindings are required")
    seen_sources = set()
    seen_reviews = set()
    for item in bindings:
        review_id = item["review_id"]
        source = item["source_block_id"]
        review = by_id.get(review_id)
        if review is None or not is_valid_learning_review(review):
            raise ValueError("Binding must reference an existing valid review")
        if not source or source in seen_sources or review_id in seen_reviews:
            raise ValueError("Binding must be one-to-one")
        seen_sources.add(source)
        seen_reviews.add(review_id)
        if review.source_block_id not in ("", source):
            raise ValueError("Existing source cannot be reassigned")
        if state.source_bindings.get(source, review_id) != review_id:
            raise ValueError("Source already belongs to another review")
        if any(k != source and v == review_id for k, v in state.source_bindings.items()):
            raise ValueError("Review already belongs to another source")
        if any(r.source_block_id == source and r.review_id != review_id for r in reviews):
            raise ValueError("Source already exists in processed")
        paths = [p for p in storage.list_files("raw") if p.endswith(f"_{review.source_page_id}.md")]
        if len(paths) != 1:
            raise ValueError("Missing or ambiguous raw")
        raw_path = paths[0]
        markdown = storage.load_text(raw_path)
        chunks = split_reviews(markdown)
        digests = [content_hash(chunk) for chunk in chunks]
        sidecar = storage.load_json(raw_path[:-3] + ".identity.json")
        records = sidecar.get("reviews", [])
        if (sidecar.get("page_id") != review.source_page_id
                or sidecar.get("page_hash") != content_hash(markdown)
                or [r.get("content_hash") for r in records] != digests
                or len({r.get("source_block_id") for r in records}) != len(records)):
            raise ValueError("Invalid raw source mapping")
        matches = [r for r in records if r.get("source_block_id") == source and r.get("content_hash") == review.content_hash]
        if len(matches) != 1 or digests.count(review.content_hash) != 1:
            raise ValueError("Source and review hash must match uniquely")
        if sum(r.source_page_id == review.source_page_id and r.content_hash == review.content_hash for r in reviews) != 1:
            raise ValueError("Ambiguous processed hash")
        bound = review.model_copy(update={"source_block_id": source})
        by_id[review_id] = bound
        update_current_review_state(state, bound)
        state.pending_sources.pop(source, None)
    state.identity_version = 2
    return [by_id[r.review_id] for r in reviews], state


def validate_snapshot(snapshot: dict) -> dict:
    """Validate complete parser boundaries and globally unique source IDs."""
    from datetime import date
    import re
    pages = {}
    sources = set()
    for page in snapshot['pages']:
        pid, title = page['page_id'], page['title']
        if pid in pages or not re.fullmatch(r'[0-9]{4}-[0-9]{2}', title):
            raise ValueError('Duplicate page or invalid month title')
        date(int(title[:4]), int(title[5:]), 1)
        if [r['markdown'] for r in page['reviews']] != split_reviews(page['markdown']):
            raise ValueError('Snapshot Markdown/source boundaries disagree')
        for record in page['reviews']:
            source = record['source_block_id']
            if not source or source in sources or content_hash(record['markdown']) != record['content_hash']:
                raise ValueError('Invalid, duplicate, or corrupt snapshot source')
            sources.add(source)
        pages[pid] = page
    return pages


def snapshot_diff(baseline: dict, latest: dict) -> dict:
    old, new = validate_snapshot(baseline), validate_snapshot(latest)
    if old.keys() != new.keys() or any(old[pid]['title'] != new[pid]['title'] for pid in old):
        raise ValueError('Snapshot page scope changed')
    def flatten(pages):
        return {r['source_block_id']:(pid, r) for pid,p in pages.items() for r in p['reviews']}
    a, b = flatten(old), flatten(new)
    added, deleted = sorted(b.keys()-a.keys()), sorted(a.keys()-b.keys())
    changed = sorted(k for k in a.keys()&b.keys() if a[k][0] != b[k][0] or a[k][1]['content_hash'] != b[k][1]['content_hash'])
    return {'added':added,'deleted':deleted,'changed':changed,'unchanged':len(a.keys()&b.keys())-len(changed)}


def prepare_restoration(original_reviews: list[Review], current_reviews: list[Review], state: FetchState,
                        baseline: dict, latest: dict) -> dict:
    """Prepare the approved historical scope in memory; new/changed live entries stay pending."""
    from src.storage.state_store import update_current_review_state, update_page_state
    old_pages, pages = validate_snapshot(baseline), validate_snapshot(latest)
    delta = snapshot_diff(baseline, latest)
    state = state.model_copy(deep=True)
    rows = {r.review_id:r.model_copy(deep=True) for r in current_reviews}
    if len(rows) != len(current_reviews): raise ValueError('Duplicate processed review IDs')
    if state.identity_version > 2: raise ValueError('Unsupported identity version')
    originals = {r.review_id:r for r in original_reviews}
    if len(originals) != len(original_reviews): raise ValueError('Duplicate original review IDs')
    old_hashes = Counter((pid,r['content_hash']) for pid,p in old_pages.items() for r in p['reviews'])
    live_hashes = Counter(r['content_hash'] for p in pages.values() for r in p['reviews'])
    live_sources = {r['source_block_id']:(pid,r) for pid,p in pages.items() for r in p['reviews']}
    comparison_models = {
        r['source_block_id']:parse_review(r['markdown'],pid,p['title'])
        for pid,p in pages.items() for r in p['reviews']
    }
    counts = Counter()
    held, excluded = [], []
    approved_sources = set()
    empty_source = '40438d68-8a87-807f-b6fb-d9e622d2f378'

    def hold(source, reason):
        state.pending_sources[source] = reason
        held.append({'source_block_id':source,'reason':reason})

    for pid,page in old_pages.items():
        for old in page['reviews']:
            source, digest = old['source_block_id'], old['content_hash']
            approved_sources.add(source)
            matches = [r for r in original_reviews if r.source_page_id == pid and r.content_hash == digest]
            record = live_sources.get(source)
            if record is None:
                hold(source, 'deleted_since_baseline'); continue
            live_pid, entry = record
            if live_pid != pid or entry['content_hash'] != digest:
                hold(source, 'changed_since_baseline'); continue
            if live_hashes[digest] != 1 or old_hashes[(pid,digest)] != 1 or len(matches) > 1:
                hold(source, 'ambiguous_hash'); continue
            parsed = parse_review(entry['markdown'], pid, page['title'])
            if source == empty_source:
                if parsed is None or is_valid_learning_review(parsed):
                    hold(source, 'empty_source_changed'); continue
                for legacy in matches:
                    current = rows.get(legacy.review_id)
                    if current and (current.content_hash != digest or is_valid_learning_review(current)):
                        raise ValueError('Refusing to exclude a changed/valid saved review')
                    if current:
                        excluded.append(current.model_dump(mode='json'))
                        del rows[legacy.review_id]
                        counts['excluded'] += 1
                    state.current_reviews.pop(legacy.review_id, None)
                state.source_bindings.pop(source, None)
                state.pending_sources.pop(source, None)
                state.excluded_sources[source] = 'empty_heading_2026-10-25'
                continue
            if parsed is None or not is_valid_learning_review(parsed):
                hold(source, 'invalid_review'); continue
            fixed = matches[0].review_id if matches else f'notion:{source}'
            if any(r.source_block_id == source and r.review_id != fixed for r in rows.values()):
                raise ValueError('Source already belongs to a different fixed ID')
            if state.source_bindings.get(source, fixed) != fixed or any(s != source and i == fixed for s,i in state.source_bindings.items()):
                raise ValueError('Fixed ID/source binding conflict')
            existing = rows.get(fixed)
            if existing and existing.source_block_id not in ('', source): raise ValueError('Fixed ID collision')
            if matches:
                if existing is None or existing.content_hash != digest:
                    hold(source, 'current_processed_changed_or_missing'); continue
                result = existing.model_copy(update={'source_block_id':source})
                counts['bound'] += 1
            else:
                # The baseline historical candidate scope is approved; copy/revision evidence is checked below.
                if existing:
                    if existing.content_hash != digest: hold(source, 'current_processed_changed'); continue
                    result = existing
                    counts['already_restored'] += 1
                else:
                    result = parse_review(entry['markdown'], pid, page['title'], fixed_review_id=fixed, source_block_id=source)
                    if _suspicious_candidate(result, original_reviews, comparison_models, source):
                        hold(source, 'similar_or_revised_candidate'); continue
                    counts['added'] += 1
            rows[fixed] = result
            current = state.current_reviews.get(fixed)
            if state.source_bindings.get(source) != fixed or current is None or current.content_hash != digest:
                update_current_review_state(state, result)
            state.pending_sources.pop(source, None)
    for source in sorted(live_sources.keys()-approved_sources):
        hold(source, 'new_since_baseline_requires_review')
    for pid,page in pages.items():
        previous = state.pages.get(pid)
        if previous is None or previous.content_hash != content_hash(page['markdown']):
            update_page_state(state,pid,page['title'],page.get('last_edited_time'),content_hash(page['markdown']))
    state.identity_version = 2
    result_rows = sorted(rows.values(),key=lambda r:(r.date,r.review_id))
    validate_restored_reviews(result_rows, state)
    return {'reviews':result_rows,'state':state,'counts':dict(counts),'held':held,'excluded':excluded,'snapshot_diff':delta}


def _suspicious_candidate(review: Review, originals: list[Review], baseline_models: dict, source: str) -> bool:
    import re
    def norm(text): return re.sub(r'\s+',' ',text).strip().casefold()
    def learning(r):
        values = [*r.good_points,*r.weak_points,*r.my_draft,*r.expressions_to_add,*r.expressions_to_use_next_time]
        values += ['|'.join((e.your_phrase,e.more_natural,e.note)) for e in r.more_natural_expressions]
        values += ['|'.join((c.phrase,c.meaning,c.example)) for c in r.phrase_cards]
        return {norm(value) for value in values if norm(value)}
    own = learning(review)
    for other_source,other in baseline_models.items():
        if other_source == source or other is None: continue
        if review.topic and norm(other.topic) == norm(review.topic): return True
        items = learning(other)
        if min(len(own),len(items)) >= 3 and len(own & items)/max(1,len(own | items)) >= .8: return True
    return any(r.content_hash == review.content_hash for r in originals)


def validate_restored_reviews(reviews: list[Review], state: FetchState) -> None:
    ids = [r.review_id for r in reviews]
    sources = [r.source_block_id for r in reviews if r.source_block_id]
    if len(ids) != len(set(ids)) or len(sources) != len(set(sources)): raise ValueError('Duplicate restored identity')
    for review in reviews:
        if not review.source_block_id: continue  # Unrelated/held legacy records are retained.
        if state.source_bindings.get(review.source_block_id) != review.review_id: raise ValueError('Binding/processed mismatch')
        current = state.current_reviews.get(review.review_id)
        if (current is None or current.content_hash != review.content_hash
                or current.date != review.date or current.source_page_id != review.source_page_id):
            raise ValueError('Current state/processed mismatch')
        for item in [*review.phrase_cards,*review.more_natural_expressions]:
            if item.source_review_id and item.source_review_id != review.review_id: raise ValueError('Broken nested review reference')
