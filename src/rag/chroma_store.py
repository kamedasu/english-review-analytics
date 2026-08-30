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


class ChromaClient(Protocol):
    def list_collections(self) -> list[Any]:
        ...

    def delete_collection(self, name: str) -> None:
        ...

    def create_collection(self, name: str, embedding_function: Any = None) -> ChromaCollection:
        ...


class ChromaStore:
    """Persistent local Chroma storage. It never performs embedding itself."""

    def __init__(self, path: Path, collection_name: str, client: ChromaClient | None = None) -> None:
        self.path = path
        self.collection_name = collection_name
        self._client = client or _persistent_client(path)

    def rebuild(self, documents: list[RagDocument], embeddings: list[list[float]]) -> int:
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
        )
        if documents:
            collection.add(
                ids=[document.id for document in documents],
                documents=[document.text for document in documents],
                embeddings=embeddings,
                metadatas=[document.metadata for document in documents],
            )
        return collection.count()


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
