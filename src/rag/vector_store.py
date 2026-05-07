from __future__ import annotations

from typing import Protocol

from src.rag.chunker import RagChunk


class VectorStore(Protocol):
    def upsert(self, chunks: list[RagChunk]) -> None:
        ...

    def search(self, query: str, k: int = 5) -> list[RagChunk]:
        ...


class InMemoryVectorStore:
    def __init__(self) -> None:
        self._chunks: list[RagChunk] = []

    def upsert(self, chunks: list[RagChunk]) -> None:
        self._chunks.extend(chunks)

    def search(self, query: str, k: int = 5) -> list[RagChunk]:
        terms = query.lower().split()
        scored: list[tuple[int, RagChunk]] = []
        for chunk in self._chunks:
            text = chunk.text.lower()
            score = sum(1 for term in terms if term in text)
            if score:
                scored.append((score, chunk))
        return [chunk for _, chunk in sorted(scored, key=lambda item: item[0], reverse=True)[:k]]
