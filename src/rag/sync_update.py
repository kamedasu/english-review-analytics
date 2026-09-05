from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from src.data_loader import LoadDebugInfo
from src.models import Review
from src.rag.chroma_store import RagIndexRebuildRequiredError
from src.rag.indexer import RagIndexResult, update_rag_index_incrementally


@dataclass(frozen=True)
class RagSyncUpdateOutcome:
    result: RagIndexResult | None = None
    error_kind: str = ""


def sync_completed_successfully(debug: LoadDebugInfo) -> bool:
    """A RAG update is allowed only after the existing sync/save flow reports no errors."""
    return debug.sync_requested and not any(status.status == "エラー" for status in debug.page_statuses)


def update_rag_after_successful_sync(
    saved_reviews: list[Review],
    updater: Callable[[list[Review]], RagIndexResult] = update_rag_index_incrementally,
) -> RagSyncUpdateOutcome:
    """Run the existing incremental indexer without affecting saved review data on failure."""
    try:
        return RagSyncUpdateOutcome(result=updater(saved_reviews))
    except RagIndexRebuildRequiredError:
        return RagSyncUpdateOutcome(error_kind="rebuild_required")
    except Exception:
        return RagSyncUpdateOutcome(error_kind="update_failed")


def run_rag_update_after_sync(
    debug: LoadDebugInfo,
    saved_reviews: list[Review],
    updater: Callable[[list[Review]], RagIndexResult] = update_rag_index_incrementally,
) -> RagSyncUpdateOutcome | None:
    """Gate the derived-data update behind the completed existing sync/save outcome."""
    if not sync_completed_successfully(debug):
        return None
    return update_rag_after_successful_sync(saved_reviews, updater)
