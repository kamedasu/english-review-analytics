"""Explicit local-only boundaries for migration rehearsals."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.config import DATA_DIR
from src.storage.local_storage import LocalStorage
from src.storage.base import StorageError


def file_inventory(root: Path, *, skip_manifest: bool = False) -> dict:
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('Symlink paths are not allowed in migration workspaces')
        if path.is_file() and not (skip_manifest and path.name == 'backup_manifest.json'):
            result[str(path.relative_to(root))] = {'size': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    return result


def require_workspace(root: Path) -> dict:
    original = root.absolute()
    root = root.resolve()
    if original != root or not root.name.startswith('data_migration_work_'):
        raise ValueError('An explicit non-symlink data_migration_work_* directory is required')
    marker = root / '.migration_workspace.json'
    if not marker.is_file() or marker.is_symlink():
        raise ValueError('Verified workspace marker is required')
    metadata = json.loads(marker.read_text())
    source = Path(metadata['source']).resolve()
    backup = Path(metadata['backup']).resolve()
    if metadata['work'] != str(root):
        raise ValueError('Workspace marker does not match the requested path')
    for protected in (source, backup, DATA_DIR.resolve()):
        if root == protected or root.is_relative_to(protected) or protected.is_relative_to(root):
            raise ValueError('Workspace overlaps production or backup')
    if any(path.is_symlink() for path in root.rglob('*')):
        raise ValueError('Symlink found in workspace')
    return metadata


class WorkspaceStorage(LocalStorage):
    def __init__(self, root_dir: Path):
        require_workspace(root_dir)
        super().__init__(root_dir.resolve())

    def _path(self, path: str) -> Path:
        target = super()._path(path)
        if not target.resolve().is_relative_to(self.root_dir):
            raise StorageError('Write/read path escapes the workspace')
        return target

    def save_text(self, path: str, content: str) -> None:
        require_workspace(self.root_dir)
        super().save_text(path, content)

    def delete(self, path: str) -> None:
        require_workspace(self.root_dir)
        super().delete(path)


def verify_protected(root: Path) -> dict:
    metadata = require_workspace(root)
    source = Path(metadata['source'])
    backup = Path(metadata['backup'])
    expected = metadata['files']
    results = {
        'production_unchanged': file_inventory(source) == expected,
        'new_backup_unchanged': file_inventory(backup, skip_manifest=True) == expected,
    }
    old = source.parent / 'data_backup_20261103_154147'
    if (old/'backup_manifest.json').is_file():
        old_manifest = json.loads((old/'backup_manifest.json').read_text())
        results['old_backup_unchanged'] = file_inventory(old, skip_manifest=True) == old_manifest['files']
    env = source.parent / '.env'
    if env.is_file() and metadata.get('env_sha256'):
        results['env_unchanged'] = hashlib.sha256(env.read_bytes()).hexdigest() == metadata['env_sha256']
    if not all(results.values()):
        raise ValueError('Protected production/backup data changed; stop rehearsal')
    return results
