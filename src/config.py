from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
import os


load_dotenv()


ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"


@dataclass(frozen=True)
class Settings:
    notion_api_key: str
    notion_page_ids: list[str]
    openai_api_key: str
    openai_model: str
    storage_mode: str
    s3_bucket: str
    s3_endpoint_url: str
    aws_access_key_id: str
    aws_secret_access_key: str


def get_settings() -> Settings:
    return Settings(
        notion_api_key=os.getenv("NOTION_API_KEY", ""),
        notion_page_ids=_split_csv(os.getenv("NOTION_PAGE_IDS", "")),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        storage_mode=os.getenv("STORAGE_MODE", "local"),
        s3_bucket=os.getenv("S3_BUCKET", ""),
        s3_endpoint_url=os.getenv("S3_ENDPOINT_URL", ""),
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", ""),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", ""),
    )


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]
