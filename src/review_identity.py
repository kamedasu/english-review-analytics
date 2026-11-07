"""Review identity decisions; no network, storage writes, or date-based replacement."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from src.models import FetchState, Review
from src.notion_client import NotionReviewBlock
from src.notion_parser import parse_review
from src.utils.hashing import content_hash
from src.storage.state_store import update_current_review_state


@dataclass
class IdentitySyncResult:
    reviews: list[Review]
    added_count: int = 0
    updated_count: int = 0
    skipped_count: int = 0
    held: list[dict[str, str]] = field(default_factory=list)


def is_valid_learning_review(review: Review) -> bool:
    learning = (
        review.my_draft or review.more_natural_version or review.why_it_was_corrected
        or review.good_points or review.weak_points or review.more_natural_expressions
        or review.phrase_cards or review.words_and_phrases_actually_used
        or review.expressions_to_add or review.expressions_to_use_next_time
    )
    return bool(learning or (review.duration_minutes > 0 and review.topic.strip()))


def sync_review_blocks(
    page_id: str,
    page_title: str,
    blocks: tuple[NotionReviewBlock, ...],
    existing_reviews: list[Review],
    state: FetchState,
    parser_warnings: list[str],
    *,
    historical_hashes: set[str] | None = None,
    parser: Callable = parse_review,
) -> IdentitySyncResult:
    result = IdentitySyncResult(reviews=list(existing_reviews))
    if state.identity_version > 2:
        raise ValueError("未対応のreview identity stateです。同期を中止しました。")
    historical_hashes = historical_hashes or set()
    block_counts = Counter(block.source_block_id for block in blocks)
    hash_counts = Counter(content_hash(block.markdown) for block in blocks)
    by_id = {review.review_id: review for review in existing_reviews}
    if len(by_id) != len(existing_reviews):
        raise ValueError("保存済みreview_idが重複しています。同期を中止しました。")
    page_reviews = [review for review in existing_reviews if review.source_page_id == page_id]
    present_ids = {block.source_block_id for block in blocks if block.source_block_id}
    missing_sources = [r for r in page_reviews if r.source_block_id and r.source_block_id not in present_ids]
    unbound_legacy = [r for r in page_reviews if not r.source_block_id]

    def hold(block, reason):
        item = {"source_block_id": block.source_block_id, "content_hash": content_hash(block.markdown), "reason": reason}
        result.held.append(item)
        if block.source_block_id:
            state.pending_sources[block.source_block_id] = reason
        parser_warnings.append(f"{page_title}: review保留 ({reason}, block={block.source_block_id or 'unknown'})")

    for block in blocks:
        source = block.source_block_id
        digest = content_hash(block.markdown)
        if not source or block_counts[source] != 1:
            hold(block, "missing_or_duplicate_heading_id")
            continue
        if source in state.excluded_sources:
            hold(block, "excluded_source")
            continue
        sticky_reason = state.pending_sources.get(source)
        if sticky_reason in {
            "historical_raw_only_candidate", "ambiguous_or_copied_review",
            "legacy_or_recreated_heading_needs_review", "legacy_id_already_bound",
            "new_since_baseline_requires_review", "changed_since_baseline",
            "deleted_since_baseline", "ambiguous_hash", "similar_or_revised_candidate",
            "current_processed_changed_or_missing", "current_processed_changed",
        } and source not in state.source_bindings:
            hold(block, sticky_reason)
            continue
        owned = [r for r in existing_reviews if r.source_block_id == source]
        bound_id = state.source_bindings.get(source, "")
        if len(owned) > 1 or (owned and bound_id and owned[0].review_id != bound_id):
            hold(block, "conflicting_source_binding")
            continue
        previous = owned[0] if owned else by_id.get(bound_id)
        if previous and previous.source_page_id != page_id:
            hold(block, "source_page_changed")
            continue
        if previous and previous.source_block_id not in ("", source):
            hold(block, "conflicting_source_binding")
            continue
        if previous and previous.source_block_id == source and previous.content_hash == digest:
            schema_refresh = (
                "Words and phrases actually used" in block.markdown
                and not previous.words_and_phrases_actually_used
                and previous.review_id not in state.current_reviews
            )
            if not schema_refresh and is_valid_learning_review(previous):
                # The processed version, rather than any historical hash, is authoritative.
                state.source_bindings[source] = previous.review_id
                current = state.current_reviews.get(previous.review_id)
                if current is None or current.content_hash != previous.content_hash:
                    update_current_review_state(state, previous)
                state.pending_sources.pop(source, None)
                result.skipped_count += 1
                continue
        if previous is None and not bound_id:
            matches = [r for r in page_reviews if r.content_hash == digest and not r.source_block_id]
            if len(matches) == 1 and hash_counts[digest] == 1:
                # No other source may already own this legacy ID.
                candidate = matches[0]
                if any(key != source and value == candidate.review_id for key, value in state.source_bindings.items()):
                    hold(block, "legacy_id_already_bound")
                    continue
                previous = candidate
            elif matches or hash_counts[digest] > 1 or any(r.content_hash == digest for r in existing_reviews):
                hold(block, "ambiguous_or_copied_review")
                continue
            elif digest in historical_hashes and sticky_reason not in {"empty_or_insufficient_review", "parse_failed"}:
                hold(block, "historical_raw_only_candidate")
                continue
            elif unbound_legacy or missing_sources:
                # Editing/recreating an unmapped heading cannot be distinguished safely from a new session.
                hold(block, "legacy_or_recreated_heading_needs_review")
                continue
        review_id = previous.review_id if previous else bound_id or f"notion:{source}"
        if any(key != source and value == review_id for key, value in state.source_bindings.items()):
            hold(block, "review_id_already_bound")
            continue
        if not previous and review_id in by_id:
            hold(block, "review_id_collision")
            continue
        try:
            schema_refresh = (
                previous and "Words and phrases actually used" in block.markdown
                and not previous.words_and_phrases_actually_used
                and previous.review_id not in state.current_reviews
            )
            if previous and previous.content_hash == digest and is_valid_learning_review(previous) and not schema_refresh:
                # Binding a legacy row must not rewrite its parsed fields or nested references.
                review = previous.model_copy(update={"source_block_id": source})
            else:
                review = parser(
                    block.markdown, page_id, page_title, parser_warnings,
                    fixed_review_id=review_id, source_block_id=source,
                )
        except (ValueError, TypeError):
            hold(block, "parse_failed")
            continue
        if review is None or not is_valid_learning_review(review):
            hold(block, "empty_or_insufficient_review")
            continue
        # A binding without processed data is recoverable with the original fixed ID.
        result.reviews = [r for r in result.reviews if r.review_id != review_id]
        result.reviews.append(review)
        if previous:
            result.updated_count += 1
        else:
            result.added_count += 1
        update_current_review_state(state, review)
        state.pending_sources.pop(source, None)
    state.identity_version = 2
    return result
