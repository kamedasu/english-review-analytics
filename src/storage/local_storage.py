from __future__ import annotations

from pathlib import Path

from src.storage.base import Storage, StorageError


class LocalStorage(Storage):
    """Existing DATA_ROOT_DIR-backed storage, addressed relative to its root."""

    def __init__(self, root_dir: Path):
        self.root_dir = root_dir

    def exists(self, path: str) -> bool:
        return self._path(path).exists()

    def load_text(self, path: str) -> str:
        try:
            return self._path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise StorageError(f"Could not read local storage file {path}: {exc}") from exc

    def save_text(self, path: str, content: str) -> None:
        target = self._path(path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise StorageError(f"Could not write local storage file {path}: {exc}") from exc

    def list_files(self, prefix: str) -> list[str]:
        root = self._path(prefix)
        if not root.exists():
            return []
        if root.is_file():
            return [prefix]
        return [str(item.relative_to(self.root_dir)) for item in root.rglob("*") if item.is_file()]

    def delete(self, path: str) -> None:
        try:
            self._path(path).unlink(missing_ok=True)
        except OSError as exc:
            raise StorageError(f"Could not delete local storage file {path}: {exc}") from exc

    def _path(self, path: str) -> Path:
        relative = Path(path)
        if relative.is_absolute() or ".." in relative.parts:
            raise StorageError(f"Storage path must be relative to DATA_ROOT_DIR: {path}")
        return self.root_dir / relative
