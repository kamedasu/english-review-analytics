from __future__ import annotations

from botocore.exceptions import BotoCoreError, ClientError
import boto3

from src.storage.base import Storage, StorageError


class S3Storage(Storage):
    def __init__(self, bucket: str, prefix: str = "data", region: str = "", endpoint_url: str = ""):
        if not bucket:
            raise StorageError("S3_BUCKET must be set when STORAGE_MODE=s3.")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        try:
            self.client = boto3.client("s3", region_name=region or None, endpoint_url=endpoint_url or None)
        except BotoCoreError as exc:
            raise StorageError(f"Could not initialize S3 storage for bucket {bucket}: {exc}") from exc

    def exists(self, path: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(path))
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise self._error("check", path, exc) from exc
        except BotoCoreError as exc:
            raise self._error("check", path, exc) from exc

    def load_text(self, path: str) -> str:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._key(path))
            return response["Body"].read().decode("utf-8")
        except (ClientError, BotoCoreError, UnicodeDecodeError) as exc:
            raise self._error("read", path, exc) from exc

    def save_text(self, path: str, content: str) -> None:
        try:
            self.client.put_object(Bucket=self.bucket, Key=self._key(path), Body=content.encode("utf-8"))
        except (ClientError, BotoCoreError) as exc:
            raise self._error("write", path, exc) from exc

    def list_files(self, prefix: str) -> list[str]:
        key_prefix = self._key(prefix).rstrip("/") + "/"
        try:
            paginator = self.client.get_paginator("list_objects_v2")
            keys = []
            for page in paginator.paginate(Bucket=self.bucket, Prefix=key_prefix):
                keys.extend(item["Key"] for item in page.get("Contents", []))
            root_prefix = f"{self.prefix}/" if self.prefix else ""
            return [key.removeprefix(root_prefix) for key in keys]
        except (ClientError, BotoCoreError) as exc:
            raise self._error("list", prefix, exc) from exc

    def delete(self, path: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self._key(path))
        except (ClientError, BotoCoreError) as exc:
            raise self._error("delete", path, exc) from exc

    def _key(self, path: str) -> str:
        cleaned = path.strip("/")
        if not cleaned or ".." in cleaned.split("/"):
            raise StorageError(f"Storage path must be a non-empty relative path: {path}")
        return f"{self.prefix}/{cleaned}" if self.prefix else cleaned

    def _error(self, operation: str, path: str, exc: Exception) -> StorageError:
        return StorageError(f"Could not {operation} s3://{self.bucket}/{self._key(path)}: {exc}")
