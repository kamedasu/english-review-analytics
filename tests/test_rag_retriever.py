from pathlib import Path
import tempfile
import unittest
from datetime import date

from src.rag.chroma_store import ChromaStore, RagIndexNotFoundError
from src.rag.retriever import (
    RagRetriever,
    RetrievedDocument,
    _is_expression_focused_query,
    _prioritize_expression_documents,
    _prioritize_learning_documents,
    _select_intent_documents,
)
from src.rag.models import RagDocument
from src.rag.query_intent import RagQueryIntent


class FakeEmbeddingProvider:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.texts = texts
        return [[0.25, 0.75]]


class FakeQueryStore:
    def __init__(self) -> None:
        self.calls: list[tuple[list[float], int, dict | None]] = []

    def query(self, query_embedding, k, where=None):
        self.calls.append((query_embedding, k, where))
        return {
            "ids": [["weak:0", "phrase:0"]],
            "documents": [["Weak point: prepositions", "Phrase: at the cafe"]],
            "metadatas": [[{"type": "weak_point", "date": "2026-01-01"}, {"type": "phrase_card"}]],
            "distances": [[0.12, 0.34]],
        }


class MissingIndexStore:
    def query(self, query_embedding, k, where=None):
        raise RagIndexNotFoundError("RAG index is not available. Run: python -m src.rag.indexer")


class FakeQueryCollection:
    def __init__(self, count: int = 2) -> None:
        self._count = count
        self.kwargs: dict = {}

    def count(self) -> int:
        return self._count

    def query(self, **kwargs):
        self.kwargs = kwargs
        return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}


class FakeQueryClient:
    def __init__(self, collection: FakeQueryCollection | None = None, missing: bool = False) -> None:
        self.collection = collection or FakeQueryCollection()
        self.missing = missing

    def get_collection(self, name, embedding_function=None):
        if self.missing:
            raise ValueError("missing")
        return self.collection


class RagRetrieverTest(unittest.TestCase):
    def test_embeds_query_and_preserves_chroma_order_and_fields(self) -> None:
        provider = FakeEmbeddingProvider()
        store = FakeQueryStore()

        results = RagRetriever(provider, store).retrieve("  coffee conversation  ", k=2)

        self.assertEqual(provider.texts, ["coffee conversation"])
        self.assertEqual(store.calls, [([0.25, 0.75], 8, None)])
        self.assertEqual([result.document.id for result in results], ["weak:0", "phrase:0"])
        self.assertEqual([result.document.text for result in results], ["Weak point: prepositions", "Phrase: at the cafe"])
        self.assertEqual(results[0].document.metadata, {"type": "weak_point", "date": "2026-01-01"})
        self.assertEqual([result.distance for result in results], [0.12, 0.34])

    def test_passes_optional_metadata_filter(self) -> None:
        store = FakeQueryStore()
        RagRetriever(FakeEmbeddingProvider(), store).retrieve("prepositions", where={"type": "weak_point"})
        self.assertEqual(store.calls[0][2], {"type": "weak_point"})
        self.assertEqual(store.calls[0][1], 5)

    def test_expression_query_adds_type_filtered_learning_candidates(self) -> None:
        store = FakeQueryStore()

        RagRetriever(FakeEmbeddingProvider(), store).retrieve("corrected expressions", k=5)

        self.assertEqual(store.calls[0][2], {"type": "more_natural_expression"})
        self.assertEqual(store.calls[1][2], {"type": "phrase_card"})
        self.assertEqual(store.calls[0][1], 60)

    def test_prioritizes_a_similarly_relevant_more_natural_expression_over_weak_point(self) -> None:
        candidates = [
            _source("weak", "weak_point", 0.20),
            _source("natural", "more_natural_expression", 0.218),
        ]

        results = _prioritize_learning_documents(candidates, k=2)

        self.assertEqual([result.document.id for result in results], ["natural", "weak"])

    def test_prioritizes_phrase_card_over_good_point_when_distances_are_close(self) -> None:
        candidates = [
            _source("good", "good_point", 0.20),
            _source("phrase", "phrase_card", 0.22),
        ]

        results = _prioritize_learning_documents(candidates, k=2)

        self.assertEqual([result.document.id for result in results], ["phrase", "good"])

    def test_does_not_promote_an_unrelated_more_natural_expression_over_a_close_weak_point(self) -> None:
        candidates = [
            _source("weak", "weak_point", 0.12),
            _source("unrelated-natural", "more_natural_expression", 0.55),
        ]

        results = _prioritize_learning_documents(candidates, k=2)

        self.assertEqual([result.document.id for result in results], ["weak", "unrelated-natural"])

    def test_weak_point_remains_first_for_a_clearly_weak_point_focused_query(self) -> None:
        candidates = [
            _source("weak", "weak_point", 0.10),
            _source("natural", "more_natural_expression", 0.18),
            _source("phrase", "phrase_card", 0.19),
        ]

        results = _prioritize_learning_documents(candidates, k=3)

        self.assertEqual(results[0].document.id, "weak")

    def test_expression_focused_query_reserves_relevant_concrete_learning_sources(self) -> None:
        semantic_candidates = [
            _source("weak-1", "weak_point", 0.89),
            _source("weak-2", "weak_point", 0.91),
            _source("good", "good_point", 0.98),
        ]
        learning_candidates = [
            _source("natural", "more_natural_expression", 1.238),
            _source("phrase", "phrase_card", 1.226),
        ]

        results = _prioritize_expression_documents(semantic_candidates, learning_candidates, k=5)

        self.assertEqual(
            [result.document.id for result in results],
            ["natural", "phrase", "weak-1", "weak-2", "good"],
        )

    def test_expression_priority_excludes_learning_candidates_outside_relevance_margin(self) -> None:
        semantic_candidates = [
            _source("weak", "weak_point", 0.12),
            _source("good", "good_point", 0.20),
        ]
        learning_candidates = [_source("unrelated-natural", "more_natural_expression", 0.60)]

        results = _prioritize_expression_documents(semantic_candidates, learning_candidates, k=2)

        self.assertEqual([result.document.id for result in results], ["weak", "good"])

    def test_expression_markers_do_not_treat_a_weakness_question_as_an_expression_question(self) -> None:
        self.assertTrue(_is_expression_focused_query("前置詞で実際にどんな英語を間違えてた？"))
        self.assertTrue(_is_expression_focused_query("過去に直された英語表現を具体例つきで教えて"))
        self.assertFalse(_is_expression_focused_query("最近の弱点は？"))

    def test_natural_expression_selection_uses_mne_then_phrase_without_good_or_weak_fillers(self) -> None:
        candidates = [
            _source("natural-1", "more_natural_expression", 0.30, "2026-08-10", "More natural: Use this."),
            _source("phrase-1", "phrase_card", 0.10, "2026-08-11", "Phrase: fallback"),
        ]
        intent = RagQueryIntent("natural_expression", 5, False, False)

        results = _select_intent_documents(candidates, intent, k=5)

        self.assertEqual([result.document.id for result in results], ["natural-1", "phrase-1"])

    def test_phrase_recommendation_does_not_use_good_or_weak_as_fillers(self) -> None:
        candidates = [
            _source("phrase-1", "phrase_card", 0.10, "2026-08-11", "Phrase: useful"),
            _source("natural-1", "more_natural_expression", 0.20, "2026-08-10", "More natural: natural"),
        ]
        intent = RagQueryIntent("phrase_recommendation", 5, False, False)

        results = _select_intent_documents(candidates, intent, k=5)

        self.assertEqual([result.document.metadata["type"] for result in results], ["phrase_card", "more_natural_expression"])

    def test_recent_selection_prefers_newer_documents_and_expands_window_when_needed(self) -> None:
        candidates = [
            _source("recent", "more_natural_expression", 0.40, "2026-08-14", "More natural: recent"),
            _source("older", "more_natural_expression", 0.10, "2026-04-20", "More natural: older"),
        ]
        intent = RagQueryIntent("natural_expression", 2, True, True)

        results = _select_intent_documents(candidates, intent, k=2)

        self.assertEqual([result.document.id for result in results], ["recent", "older"])

    def test_deduplicates_phrase_and_more_natural_values(self) -> None:
        candidates = [
            _source("phrase-1", "phrase_card", 0.10, "2026-08-14", "Phrase: Wind down!"),
            _source("phrase-2", "phrase_card", 0.20, "2026-08-13", "Phrase: wind down"),
            _source("natural-1", "more_natural_expression", 0.30, "2026-08-12", "Your phrase: x\nMore natural: Keep it up."),
            _source("natural-2", "more_natural_expression", 0.40, "2026-08-11", "Your phrase: y\nMore natural: keep it up"),
        ]
        intent = RagQueryIntent("phrase_recommendation", 5, False, False)

        results = _select_intent_documents(candidates, intent, k=5)

        self.assertEqual([result.document.id for result in results], ["phrase-1", "natural-1"])

    def test_explicit_date_range_is_a_hard_filter_even_when_requested_count_is_not_met(self) -> None:
        candidates = [
            _source("july-natural", "more_natural_expression", 0.40, "2026-07-14", "More natural: July only"),
            _source("july-phrase", "phrase_card", 0.50, "2026-07-03", "Phrase: July fallback"),
            _source("june-natural", "more_natural_expression", 0.01, "2026-06-14", "More natural: outside"),
            _source("april-phrase", "phrase_card", 0.02, "2026-04-24", "Phrase: outside"),
        ]
        intent = RagQueryIntent("natural_expression", 10, True, True, date(2026, 7, 1), date(2026, 7, 31))

        results = _select_intent_documents(candidates, intent, k=10)

        self.assertEqual([result.document.id for result in results], ["july-natural", "july-phrase"])
        self.assertTrue(all(result.document.metadata["date"].startswith("2026-07") for result in results))

    def test_explicit_date_range_can_use_secondary_type_but_not_good_or_weak_fillers(self) -> None:
        candidates = [
            _source("july-phrase", "phrase_card", 0.10, "2026-07-20", "Phrase: useful"),
            _source("july-natural", "more_natural_expression", 0.20, "2026-07-19", "More natural: natural alternative"),
        ]
        intent = RagQueryIntent("phrase_recommendation", 10, False, True, date(2026, 7, 1), date(2026, 7, 31))

        results = _select_intent_documents(candidates, intent, k=10)

        self.assertEqual([result.document.metadata["type"] for result in results], ["phrase_card", "more_natural_expression"])

    def test_rejects_blank_query_and_non_positive_k_without_embedding(self) -> None:
        provider = FakeEmbeddingProvider()
        retriever = RagRetriever(provider, FakeQueryStore())
        with self.assertRaisesRegex(ValueError, "Query must not be blank"):
            retriever.retrieve("   ")
        with self.assertRaisesRegex(ValueError, "k must be greater than zero"):
            retriever.retrieve("valid", k=0)
        self.assertEqual(provider.texts, [])

    def test_surfaces_a_clear_missing_index_error(self) -> None:
        with self.assertRaisesRegex(RagIndexNotFoundError, "python -m src.rag.indexer"):
            RagRetriever(FakeEmbeddingProvider(), MissingIndexStore()).retrieve("valid")

    def test_chroma_store_uses_query_embeddings_not_query_texts_and_clamps_k(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            collection = FakeQueryCollection(count=2)
            store = ChromaStore(Path(directory), "english_review_documents", FakeQueryClient(collection))
            store.query([0.1, 0.2], k=5, where={"type": "weak_point"})

        self.assertEqual(collection.kwargs["query_embeddings"], [[0.1, 0.2]])
        self.assertEqual(collection.kwargs["n_results"], 2)
        self.assertNotIn("query_texts", collection.kwargs)
        self.assertEqual(collection.kwargs["where"], {"type": "weak_point"})

    def test_chroma_store_reports_a_missing_collection_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ChromaStore(Path(directory), "english_review_documents", FakeQueryClient(missing=True))
            with self.assertRaisesRegex(RagIndexNotFoundError, "python -m src.rag.indexer"):
                store.query([0.1], k=1)


def _source(
    document_id: str,
    document_type: str,
    distance: float,
    document_date: str = "2026-01-01",
    text: str | None = None,
) -> RetrievedDocument:
    return RetrievedDocument(
        RagDocument(document_id, text or f"{document_type}: {document_id}", {"type": document_type, "date": document_date}),
        distance,
    )
