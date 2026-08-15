from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class StorageError(RuntimeError):
    """Raised when the configured persistent storage cannot be used safely."""


class Storage(ABC):
    @abstractmethod
    def exists(self, path: str) -> bool: ...

    @abstractmethod
    def load_text(self, path: str) -> str: ...

    @abstractmethod
    def save_text(self, path: str, content: str) -> None: ...

    @abstractmethod
    def list_files(self, prefix: str) -> list[str]: ...

    @abstractmethod
    def delete(self, path: str) -> None: ...

    def load_json(self, path: str) -> Any:
        import json

        try:
            return json.loads(self.load_text(path))
        except json.JSONDecodeError as exc:
            raise StorageError(f"Invalid JSON in {path}: {exc}") from exc

    def save_json(self, path: str, data: Any) -> None:
        import json

        self.save_text(path, json.dumps(data, ensure_ascii=False, indent=2))
