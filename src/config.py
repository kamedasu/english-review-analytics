from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
import os


load_dotenv()


# Compatibility defaults: changing OPENAI_MODEL never implicitly changes RAG/embedding.
DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"
DEFAULT_RAG_MODEL = "gpt-4.1-mini"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"

ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("DATA_ROOT_DIR", "")).expanduser() if os.getenv("DATA_ROOT_DIR") else ROOT_DIR / "data"


@dataclass(frozen=True)
class Settings:
    notion_api_key: str
    notion_page_ids: list[str]
    openai_api_key: str
    openai_model: str
    openai_summary_retry_count: int
    openai_rag_model: str
    openai_embedding_model: str
    rag_embedding_batch_size: int
    rag_chroma_dir: Path
    rag_collection_name: str
    storage_mode: str
    s3_bucket: str
    s3_prefix: str
    aws_region: str
    s3_endpoint_url: str
    aws_access_key_id: str
    aws_secret_access_key: str


def get_settings() -> Settings:
    return Settings(
        notion_api_key=os.getenv("NOTION_API_KEY", ""),
        notion_page_ids=_split_csv(os.getenv("NOTION_PAGE_IDS", "")),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_model=_model_setting("OPENAI_MODEL", DEFAULT_OPENAI_MODEL),
        openai_summary_retry_count=_safe_int(os.getenv("OPENAI_SUMMARY_RETRY_COUNT", ""), default=3),
        openai_rag_model=_model_setting("OPENAI_RAG_MODEL", DEFAULT_RAG_MODEL),
        openai_embedding_model=_model_setting("OPENAI_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
        rag_embedding_batch_size=_safe_positive_int(os.getenv("RAG_EMBEDDING_BATCH_SIZE", ""), default=64),
        rag_chroma_dir=_rag_chroma_dir(),
        rag_collection_name=os.getenv("RAG_COLLECTION_NAME", "english_review_documents").strip()
        or "english_review_documents",
        storage_mode=os.getenv("STORAGE_MODE", "local").strip().lower(),
        s3_bucket=os.getenv("S3_BUCKET", ""),
        s3_prefix=os.getenv("S3_PREFIX", "data").strip("/"),
        aws_region=os.getenv("AWS_REGION", "ap-northeast-1"),
        s3_endpoint_url=os.getenv("S3_ENDPOINT_URL", ""),
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", ""),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", ""),
    )


def _model_setting(name: str, default: str) -> str:
    # Blank example entries behave like unset variables. Availability/access is
    # validated by the API, not an allowlist that would reject future model IDs.
    return os.getenv(name, "").strip() or default


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _safe_int(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_positive_int(value: str, default: int) -> int:
    parsed = _safe_int(value, default)
    return parsed if parsed > 0 else default


def _rag_chroma_dir() -> Path:
    configured = os.getenv("RAG_CHROMA_DIR", "").strip()
    return Path(configured).expanduser() if configured else DATA_DIR / "chroma"
