from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from src.rag.models import RagDocument


class ChromaCollection(Protocol):
    def add(
        self,
        *,
        ids: list[str],
        documents: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, str | int | float | bool]],
    ) -> None:
        ...

    def count(self) -> int:
        ...

    def get(self, *, include: list[str]) -> dict[str, list[Any]]:
        ...

    def delete(self, *, ids: list[str]) -> None:
        ...

    def query(
        self,
        *,
        query_embeddings: list[list[float]],
        n_results: int,
        include: list[str],
        where: dict[str, str | int | float | bool] | None = None,
    ) -> dict[str, list[list[Any]]]:
        ...


class ChromaClient(Protocol):
    def list_collections(self) -> list[Any]:
        ...

    def delete_collection(self, name: str) -> None:
        ...

    def create_collection(
        self,
        name: str,
        embedding_function: Any = None,
        metadata: dict[str, str] | None = None,
    ) -> ChromaCollection:
        ...

    def get_collection(self, name: str, embedding_function: Any = None) -> ChromaCollection:
        ...


class RagIndexNotFoundError(RuntimeError):
    """Raised when the configured local RAG collection has not been built."""


class RagIndexRebuildRequiredError(RuntimeError):
    """Raised when index metadata is incompatible with an incremental update."""


class ChromaStore:
    """Persistent local Chroma storage. It never performs embedding itself."""

    def __init__(self, path: Path, collection_name: str, client: ChromaClient | None = None) -> None:
        self.path = path
        self.collection_name = collection_name
        self._client = client or _persistent_client(path)

    def rebuild(
        self,
        documents: list[RagDocument],
        embeddings: list[list[float]],
        collection_metadata: dict[str, str] | None = None,
    ) -> int:
        _validate_lengths(documents, embeddings)
        existing_names = {
            collection.name if hasattr(collection, "name") else str(collection)
            for collection in self._client.list_collections()
        }
        if self.collection_name in existing_names:
            self._client.delete_collection(self.collection_name)
        collection = self._client.create_collection(
            name=self.collection_name,
            embedding_function=None,
            metadata=collection_metadata,
        )
        if documents:
            collection.add(
                ids=[document.id for document in documents],
                documents=[document.text for document in documents],
                embeddings=embeddings,
                metadatas=[document.metadata for document in documents],
            )
        return collection.count()

    def collection_exists(self) -> bool:
        return self.collection_name in self._collection_names()

    def get_all_documents(self) -> list[RagDocument]:
        """Return the stored source fields needed for deterministic incremental diffing."""
        collection = self._get_collection()
        payload = collection.get(include=["documents", "metadatas"])
        ids = payload.get("ids", [])
        documents = payload.get("documents", [])
        metadatas = payload.get("metadatas", [])
        if not (len(ids) == len(documents) == len(metadatas)):
            raise ValueError("Chroma get returned mismatched document field counts.")
        result: list[RagDocument] = []
        for document_id, text, metadata in zip(ids, documents, metadatas):
            if not isinstance(document_id, str) or not isinstance(text, str) or not isinstance(metadata, dict):
                raise ValueError("Chroma get returned an invalid document result.")
            result.append(RagDocument(id=document_id, text=text, metadata=metadata))
        return result

    def collection_metadata(self) -> dict[str, str]:
        collection = self._get_collection()
        metadata = getattr(collection, "metadata", None) or {}
        if not isinstance(metadata, dict):
            raise ValueError("Chroma collection metadata is invalid.")
        return {str(key): str(value) for key, value in metadata.items()}

    def apply_incremental(
        self,
        delete_ids: list[str],
        documents: list[RagDocument],
        embeddings: list[list[float]],
    ) -> int:
        _validate_lengths(documents, embeddings)
        collection = self._get_collection()
        if delete_ids:
            collection.delete(ids=delete_ids)
        if documents:
            collection.add(
                ids=[document.id for document in documents],
                documents=[document.text for document in documents],
                embeddings=embeddings,
                metadatas=[document.metadata for document in documents],
            )
        return collection.count()

    def query(
        self,
        query_embedding: list[float],
        k: int,
        where: dict[str, str | int | float | bool] | None = None,
    ) -> dict[str, list[list[Any]]]:
        """Search the existing collection with a caller-provided embedding."""
        collection = self._get_collection()

        result_count = min(k, collection.count())
        if result_count == 0:
            return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}
        kwargs: dict[str, Any] = {
            "query_embeddings": [query_embedding],
            "n_results": result_count,
            "include": ["documents", "metadatas", "distances"],
        }
        if where is not None:
            kwargs["where"] = where
        return collection.query(**kwargs)

    def _collection_names(self) -> set[str]:
        return {
            collection.name if hasattr(collection, "name") else str(collection)
            for collection in self._client.list_collections()
        }

    def _get_collection(self) -> ChromaCollection:
        if not self.path.exists():
            raise _index_not_found()
        try:
            return self._client.get_collection(
                name=self.collection_name,
                embedding_function=None,
            )
        except Exception as exc:
            raise _index_not_found() from exc


def _persistent_client(path: Path) -> ChromaClient:
    try:
        import chromadb
    except ImportError as exc:
        raise RuntimeError("chromadb is required. Install dependencies with pip install -r requirements.txt.") from exc
    path.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(path))


def _validate_lengths(documents: list[RagDocument], embeddings: list[list[float]]) -> None:
    if len(documents) != len(embeddings):
        raise ValueError(
            f"Document and embedding counts must match: {len(documents)} documents, {len(embeddings)} embeddings."
        )


def _index_not_found() -> RagIndexNotFoundError:
    return RagIndexNotFoundError("RAG index is not available. Run: python -m src.rag.indexer")
