from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import re

from src.config import DATA_DIR, get_settings
from src.models import Review
from src.notion_client import NotionClient, NotionClientError
from src.notion_parser import parse_reviews_from_markdown
from src.storage.local_store import load_reviews, save_reviews
from src.storage.state_store import load_state, save_state, should_process_page, update_page_state
from src.utils.hashing import content_hash


@dataclass(frozen=True)
class PageLoadStatus:
    page_id: str
    title: str = ""
    status: str = ""
    review_count: int = 0
    last_edited_time: str | None = None
    raw_markdown_path: str = ""
    error: str = ""
    parser_warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LoadDebugInfo:
    refresh_requested: bool
    cache_event: str = "fresh fetch"
    loaded_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    page_statuses: list[PageLoadStatus] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LoadResult:
    reviews: list[Review]
    debug: LoadDebugInfo


def load_or_fetch_reviews(refresh: bool = False) -> LoadResult:
    messages: list[str] = []
    settings = get_settings()

    if not settings.notion_api_key:
        messages.append("NOTION_API_KEY が設定されていません。")
    if not settings.notion_page_ids:
        messages.append("NOTION_PAGE_IDS が設定されていません。")
    if messages:
        return LoadResult(
            reviews=[],
            debug=LoadDebugInfo(
                refresh_requested=refresh,
                page_statuses=[PageLoadStatus(page_id="", status="エラー", error="\n".join(messages))],
                messages=messages,
            ),
        )

    cached_reviews = load_reviews()
    state = load_state()
    client = NotionClient(settings.notion_api_key)
    all_reviews = cached_reviews
    changed_page_ids: set[str] = set()
    page_statuses: list[PageLoadStatus] = []

    for page_id in settings.notion_page_ids:
        try:
            page = client.fetch_page_as_markdown(page_id)
        except NotionClientError as exc:
            error = str(exc)
            messages.append(error)
            page_statuses.append(PageLoadStatus(page_id=page_id, status="エラー", error=error))
            continue

        page_hash = content_hash(page.markdown)
        raw_markdown_path = _save_raw_markdown(page.page_id, page.title, page.markdown)
        if not refresh and not should_process_page(state, page.page_id, page.last_edited_time, page_hash):
            review_count = _count_reviews_for_page(all_reviews, page.page_id)
            messages.append(f"{page.title}: 変更なし")
            page_statuses.append(
                PageLoadStatus(
                    page_id=page.page_id,
                    title=page.title,
                    status="変更なし",
                    review_count=review_count,
                    last_edited_time=page.last_edited_time,
                    raw_markdown_path=str(raw_markdown_path),
                )
            )
            continue

        changed_page_ids.add(page.page_id)
        parser_warnings: list[str] = []
        page_reviews = parse_reviews_from_markdown(
            page.markdown,
            page.page_id,
            page.title,
            warnings=parser_warnings,
        )
        all_reviews = [review for review in all_reviews if review.source_page_id != page.page_id]
        all_reviews.extend(page_reviews)
        update_page_state(state, page.page_id, page.title, page.last_edited_time, page_hash)
        messages.append(f"{page.title}: {len(page_reviews)} 件のレビューを取得")
        messages.extend(parser_warnings)
        page_statuses.append(
            PageLoadStatus(
                page_id=page.page_id,
                title=page.title,
                status="再取得",
                review_count=len(page_reviews),
                last_edited_time=page.last_edited_time,
                raw_markdown_path=str(raw_markdown_path),
                parser_warnings=parser_warnings,
            )
        )

    if changed_page_ids:
        save_reviews(all_reviews)
        save_state(state)

    if not all_reviews:
        messages.append("Notionからレビューを取得できませんでした。サイドバーの取得メッセージとdata/raw/を確認してください。")
        return LoadResult(
            reviews=[],
            debug=LoadDebugInfo(
                refresh_requested=refresh,
                page_statuses=page_statuses,
                messages=messages,
            ),
        )

    return LoadResult(
        reviews=sorted(all_reviews, key=lambda review: review.date),
        debug=LoadDebugInfo(
            refresh_requested=refresh,
            page_statuses=page_statuses,
            messages=messages,
        ),
    )


def _save_raw_markdown(page_id: str, title: str, markdown: str) -> Path:
    path = DATA_DIR / "raw" / f"{_safe_filename(title or page_id)}_{page_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    return path


def _safe_filename(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "_", value.strip())
    return normalized.strip("_")[:80] or "notion_page"


def _count_reviews_for_page(reviews: list[Review], page_id: str) -> int:
    return sum(1 for review in reviews if review.source_page_id == page_id)
