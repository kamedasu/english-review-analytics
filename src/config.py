from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
import os


load_dotenv()


ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("DATA_ROOT_DIR", "")).expanduser() if os.getenv("DATA_ROOT_DIR") else ROOT_DIR / "data"


@dataclass(frozen=True)
class Settings:
    notion_api_key: str
    notion_page_ids: list[str]
    notion_active_page_ids: list[str]
    active_months: list[str]
    archived_months: list[str]
    openai_api_key: str
    openai_model: str
    openai_summary_retry_count: int
    storage_mode: str
    s3_bucket: str
    s3_endpoint_url: str
    aws_access_key_id: str
    aws_secret_access_key: str


def get_settings() -> Settings:
    return Settings(
        notion_api_key=os.getenv("NOTION_API_KEY", ""),
        notion_page_ids=_split_csv(os.getenv("NOTION_PAGE_IDS", "")),
        notion_active_page_ids=_split_csv(os.getenv("NOTION_ACTIVE_PAGE_IDS", "")),
        active_months=_split_csv(os.getenv("ACTIVE_MONTHS", "")),
        archived_months=_split_csv(os.getenv("ARCHIVED_MONTHS", "")),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        openai_summary_retry_count=_safe_int(os.getenv("OPENAI_SUMMARY_RETRY_COUNT", ""), default=3),
        storage_mode=os.getenv("STORAGE_MODE", "local"),
        s3_bucket=os.getenv("S3_BUCKET", ""),
        s3_endpoint_url=os.getenv("S3_ENDPOINT_URL", ""),
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", ""),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", ""),
    )


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _safe_int(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
