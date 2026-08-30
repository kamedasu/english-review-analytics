from __future__ import annotations

from typing import Protocol

import requests

from src.config import get_settings


OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"


class EmbeddingProvider(Protocol):
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        ...


class EmbeddingError(RuntimeError):
    """Raised when a complete embedding result cannot be produced."""


class OpenAIEmbeddingProvider:
    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-3-small",
        batch_size: int = 64,
    ) -> None:
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required to build the RAG index.")
        if batch_size <= 0:
            raise ValueError("Embedding batch_size must be greater than zero.")
        self._api_key = api_key
        self._model = model
        self._batch_size = batch_size

    @classmethod
    def from_settings(cls) -> OpenAIEmbeddingProvider:
        settings = get_settings()
        return cls(
            api_key=settings.openai_api_key,
            model=settings.openai_embedding_model,
            batch_size=settings.rag_embedding_batch_size,
        )

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        embeddings: list[list[float]] = []
        for batch_number, start in enumerate(range(0, len(texts), self._batch_size), start=1):
            batch = texts[start : start + self._batch_size]
            try:
                embeddings.extend(self._embed_batch(batch))
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                raise EmbeddingError(
                    f"Embedding batch {batch_number} (documents {start + 1}-{start + len(batch)}) failed: {exc}"
                ) from exc
        if len(embeddings) != len(texts):
            raise EmbeddingError(
                f"Embedding count mismatch: received {len(embeddings)} vectors for {len(texts)} texts."
            )
        return embeddings

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        response = requests.post(
            OPENAI_EMBEDDINGS_URL,
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
            json={"model": self._model, "input": texts},
            timeout=60,
        )
        if response.status_code >= 400:
            raise ValueError(f"OpenAI embeddings API returned {response.status_code}: {response.text[:500]}")
        payload = response.json()
        rows = sorted(payload["data"], key=lambda item: item["index"])
        vectors = [item["embedding"] for item in rows]
        if len(vectors) != len(texts):
            raise ValueError(f"API returned {len(vectors)} vectors for {len(texts)} texts")
        return vectors


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed texts with the configured OpenAI provider."""
    return OpenAIEmbeddingProvider.from_settings().embed_texts(texts)
