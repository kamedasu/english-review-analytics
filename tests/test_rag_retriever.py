from pathlib import Path
import tempfile
import unittest

from src.rag.chroma_store import ChromaStore, RagIndexNotFoundError
from src.rag.retriever import (
    RagRetriever,
    RetrievedDocument,
    _is_expression_focused_query,
    _prioritize_expression_documents,
    _prioritize_learning_documents,
)
from src.rag.models import RagDocument


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

        results = RagRetriever(provider, store).retrieve("  preposition mistakes  ", k=2)

        self.assertEqual(provider.texts, ["preposition mistakes"])
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

        RagRetriever(FakeEmbeddingProvider(), store).retrieve("corrected English expression", k=5)

        self.assertEqual(store.calls[0][2], None)
        self.assertEqual(store.calls[1][2], {"type": "more_natural_expression"})
        self.assertEqual(store.calls[2][2], {"type": "phrase_card"})

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


def _source(document_id: str, document_type: str, distance: float) -> RetrievedDocument:
    return RetrievedDocument(
        RagDocument(document_id, f"{document_type}: {document_id}", {"type": document_type}),
        distance,
    )
