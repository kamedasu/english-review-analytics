from __future__ import annotations

from src.config import DATA_DIR, get_settings
from src.storage.base import Storage, StorageError
from src.storage.local_storage import LocalStorage
from src.storage.s3_storage import S3Storage


def get_storage() -> Storage:
    settings = get_settings()
    if settings.storage_mode == "local":
        return LocalStorage(DATA_DIR)
    if settings.storage_mode == "s3":
        return S3Storage(
            bucket=settings.s3_bucket,
            prefix=settings.s3_prefix,
            region=settings.aws_region,
            endpoint_url=settings.s3_endpoint_url,
        )
    raise StorageError("STORAGE_MODE must be either 'local' or 's3'.")
