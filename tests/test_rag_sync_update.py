import unittest

from src.data_loader import LoadDebugInfo, PageLoadStatus
from src.rag.chroma_store import RagIndexRebuildRequiredError
from src.rag.indexer import RagIndexResult
from src.rag.sync_update import run_rag_update_after_sync, sync_completed_successfully


def _result(added: int = 0, changed: int = 0, deleted: int = 0, embedded: int = 0) -> RagIndexResult:
    return RagIndexResult(
        review_count=1,
        document_count=10,
        embedded_count=embedded,
        stored_count=10,
        collection_name="english_review_documents",
        chroma_path="data/chroma",
        type_counts={},
        added_count=added,
        changed_count=changed,
        deleted_count=deleted,
        unchanged_count=10 - added - changed,
    )


class RagSyncUpdateTest(unittest.TestCase):
    def test_successful_sync_calls_incremental_update_once(self) -> None:
        calls: list[list] = []
        debug = LoadDebugInfo(sync_requested=True, page_statuses=[PageLoadStatus(page_id="page", status="同期済み")])

        outcome = run_rag_update_after_sync(debug, [], lambda reviews: calls.append(reviews) or _result(3, 2, 1, 5))

        self.assertEqual(calls, [[]])
        self.assertEqual((outcome.result.added_count, outcome.result.changed_count, outcome.result.deleted_count, outcome.result.embedded_count), (3, 2, 1, 5))

    def test_sync_or_review_save_failure_skips_incremental_update(self) -> None:
        calls: list[list] = []
        updater = lambda reviews: calls.append(reviews) or _result()
        sync_failure = LoadDebugInfo(sync_requested=True, page_statuses=[PageLoadStatus(page_id="page", status="エラー")])
        save_failure = LoadDebugInfo(sync_requested=True, page_statuses=[PageLoadStatus(page_id="", status="エラー", error="save failed")])

        self.assertFalse(sync_completed_successfully(sync_failure))
        self.assertFalse(sync_completed_successfully(save_failure))
        self.assertIsNone(run_rag_update_after_sync(sync_failure, [], updater))
        self.assertIsNone(run_rag_update_after_sync(save_failure, [], updater))
        self.assertEqual(calls, [])

    def test_normal_rerun_without_sync_skips_incremental_update(self) -> None:
        calls: list[list] = []

        outcome = run_rag_update_after_sync(
            LoadDebugInfo(sync_requested=False),
            [],
            lambda reviews: calls.append(reviews) or _result(),
        )

        self.assertIsNone(outcome)
        self.assertEqual(calls, [])

    def test_no_op_result_is_preserved_without_special_embedding_call(self) -> None:
        debug = LoadDebugInfo(sync_requested=True)

        outcome = run_rag_update_after_sync(debug, [], lambda reviews: _result())

        self.assertEqual(outcome.result.embedded_count, 0)
        self.assertEqual(outcome.result.added_count, 0)
        self.assertEqual(outcome.result.stored_count, 10)

    def test_rag_update_failure_does_not_change_the_saved_review_list(self) -> None:
        saved_reviews: list = ["saved-review"]
        debug = LoadDebugInfo(sync_requested=True)

        outcome = run_rag_update_after_sync(debug, saved_reviews, lambda reviews: (_ for _ in ()).throw(RuntimeError("failed")))

        self.assertEqual(outcome.error_kind, "update_failed")
        self.assertEqual(saved_reviews, ["saved-review"])

    def test_model_or_schema_mismatch_requires_rebuild_without_running_one(self) -> None:
        debug = LoadDebugInfo(sync_requested=True)
        calls: list[list] = []

        def mismatch(reviews):
            calls.append(reviews)
            raise RagIndexRebuildRequiredError("full rebuild required")

        outcome = run_rag_update_after_sync(debug, [], mismatch)

        self.assertEqual(outcome.error_kind, "rebuild_required")
        self.assertEqual(calls, [[]])
