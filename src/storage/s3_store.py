from __future__ import annotations

from dataclasses import dataclass

import boto3


@dataclass(frozen=True)
class S3Config:
    bucket: str
    endpoint_url: str = ""


class S3Store:
    def __init__(self, config: S3Config):
        if not config.bucket:
            raise ValueError("S3 bucket is required.")
        self.config = config
        self.client = boto3.client("s3", endpoint_url=config.endpoint_url or None)

    def put_text(self, key: str, text: str) -> None:
        self.client.put_object(Bucket=self.config.bucket, Key=key, Body=text.encode("utf-8"))

    def get_text(self, key: str) -> str:
        response = self.client.get_object(Bucket=self.config.bucket, Key=key)
        return response["Body"].read().decode("utf-8")
