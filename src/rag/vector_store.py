from __future__ import annotations

from typing import Protocol

from src.rag.models import RagDocument


class VectorStore(Protocol):
    def upsert(self, documents: list[RagDocument]) -> None:
        ...

    def search(self, query: str, k: int = 5) -> list[RagDocument]:
        ...


class InMemoryVectorStore:
    def __init__(self) -> None:
        self._documents: list[RagDocument] = []

    def upsert(self, documents: list[RagDocument]) -> None:
        self._documents.extend(documents)

    def search(self, query: str, k: int = 5) -> list[RagDocument]:
        terms = query.lower().split()
        scored: list[tuple[int, RagDocument]] = []
        for document in self._documents:
            text = document.text.lower()
            score = sum(1 for term in terms if term in text)
            if score:
                scored.append((score, document))
        return [document for _, document in sorted(scored, key=lambda item: item[0], reverse=True)[:k]]
