from pathlib import Path
import tempfile
import unittest

from src.rag.chroma_store import ChromaStore, RagIndexNotFoundError
from src.rag.retriever import RagRetriever


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
        self.assertEqual(store.calls, [([0.25, 0.75], 2, None)])
        self.assertEqual([result.document.id for result in results], ["weak:0", "phrase:0"])
        self.assertEqual([result.document.text for result in results], ["Weak point: prepositions", "Phrase: at the cafe"])
        self.assertEqual(results[0].document.metadata, {"type": "weak_point", "date": "2026-01-01"})
        self.assertEqual([result.distance for result in results], [0.12, 0.34])

    def test_passes_optional_metadata_filter(self) -> None:
        store = FakeQueryStore()
        RagRetriever(FakeEmbeddingProvider(), store).retrieve("prepositions", where={"type": "weak_point"})
        self.assertEqual(store.calls[0][2], {"type": "weak_point"})

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
