from __future__ import annotations

from dataclasses import dataclass


RagMetadataValue = str | int | float | bool


@dataclass(frozen=True)
class RagDocument:
    """A Chroma-ready, semantically scoped RAG source document."""

    id: str
    text: str
    metadata: dict[str, RagMetadataValue]
