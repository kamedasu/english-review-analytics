"""Read-only inventory for the reviewed, separately scheduled identity migration."""
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
