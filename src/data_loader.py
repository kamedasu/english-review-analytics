from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import re

from src.config import DATA_DIR, get_settings
from src.models import Review
from src.notion_client import NotionClient, NotionClientError
from src.notion_parser import parse_review, split_reviews
from src.storage.local_store import load_reviews, save_reviews
from src.storage.state_store import (
    has_processed_review,
    load_state,
    save_state,
    update_page_state,
    update_review_state,
)
from src.utils.dates import parse_date
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
    synced_month: str = ""
    added_count: int = 0
    updated_count: int = 0
    skipped_count: int = 0


@dataclass(frozen=True)
class LoadDebugInfo:
    refresh_requested: bool = False
    sync_requested: bool = False
    cache_event: str = "fresh fetch"
    loaded_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    page_statuses: list[PageLoadStatus] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LoadResult:
    reviews: list[Review]
    debug: LoadDebugInfo


def load_local_reviews() -> LoadResult:
    reviews = sorted(load_reviews(), key=lambda review: review.date)
    messages = [f"ローカル保存済みデータを表示しています: {len(reviews)} reviews"]
    if not reviews:
        messages.append("ローカル保存済みレビューがありません。必要に応じて Sync from Notion を実行してください。")
    return LoadResult(
        reviews=reviews,
        debug=LoadDebugInfo(messages=messages),
    )


def load_or_fetch_reviews(refresh: bool = False) -> LoadResult:
    if refresh:
        return sync_active_reviews_from_notion()
    return load_local_reviews()


def sync_active_reviews_from_notion() -> LoadResult:
    messages: list[str] = []
    settings = get_settings()

    if not settings.notion_api_key:
        messages.append("NOTION_API_KEY が設定されていません。")
    state = load_state()
    active_page_ids = _active_page_ids(settings, state, messages)
    if not active_page_ids and not messages:
        messages.append("NOTION_ACTIVE_PAGE_IDS または NOTION_PAGE_IDS が設定されていません。")
    if messages:
        return LoadResult(
            reviews=sorted(load_reviews(), key=lambda review: review.date),
            debug=LoadDebugInfo(
                sync_requested=True,
                page_statuses=[PageLoadStatus(page_id="", status="エラー", error="\n".join(messages))],
                messages=messages,
            ),
        )

    cached_reviews = load_reviews()
    for review in cached_reviews:
        update_review_state(state, review)
    client = NotionClient(settings.notion_api_key)
    all_reviews = cached_reviews
    page_statuses: list[PageLoadStatus] = []

    for page_id in active_page_ids:
        try:
            page = client.fetch_page_as_markdown(page_id)
        except NotionClientError as exc:
            error = str(exc)
            messages.append(error)
            page_statuses.append(PageLoadStatus(page_id=page_id, status="エラー", error=error))
            continue

        page_hash = content_hash(page.markdown)
        raw_markdown_path = _save_raw_markdown(page.page_id, page.title, page.markdown)
        parser_warnings: list[str] = []
        sync_result = _sync_page_reviews(
            page_id=page.page_id,
            page_title=page.title,
            markdown=page.markdown,
            existing_reviews=all_reviews,
            state=state,
            parser_warnings=parser_warnings,
        )
        all_reviews = sync_result["reviews"]
        update_page_state(state, page.page_id, page.title, page.last_edited_time, page_hash)
        messages.append(
            f"{page.title}: added {sync_result['added_count']}, "
            f"updated {sync_result['updated_count']}, skipped {sync_result['skipped_count']}"
        )
        messages.extend(parser_warnings)
        page_statuses.append(
            PageLoadStatus(
                page_id=page.page_id,
                title=page.title,
                status="同期済み",
                review_count=_count_reviews_for_page(all_reviews, page.page_id),
                last_edited_time=page.last_edited_time,
                raw_markdown_path=str(raw_markdown_path),
                parser_warnings=parser_warnings,
                synced_month=_month_from_title_or_reviews(page.title, all_reviews, page.page_id),
                added_count=sync_result["added_count"],
                updated_count=sync_result["updated_count"],
                skipped_count=sync_result["skipped_count"],
            )
        )

    save_reviews(all_reviews)
    save_state(state)

    if not all_reviews:
        messages.append("レビューを取得できませんでした。サイドバーの取得メッセージとdata/raw/を確認してください。")
        return LoadResult(
            reviews=[],
            debug=LoadDebugInfo(
                sync_requested=True,
                page_statuses=page_statuses,
                messages=messages,
            ),
        )

    return LoadResult(
        reviews=sorted(all_reviews, key=lambda review: review.date),
        debug=LoadDebugInfo(
            sync_requested=True,
            page_statuses=page_statuses,
            messages=messages,
        ),
    )


def _sync_page_reviews(
    page_id: str,
    page_title: str,
    markdown: str,
    existing_reviews: list[Review],
    state,
    parser_warnings: list[str],
) -> dict:
    reviews = list(existing_reviews)
    added_count = 0
    updated_count = 0
    skipped_count = 0

    for chunk in split_reviews(markdown):
        chunk_hash = content_hash(chunk)
        review_date = _review_date_from_chunk(chunk)
        if not review_date:
            parser_warnings.append(f"{page_title}: review date not found in a chunk.")
            continue
        if has_processed_review(state, page_id, review_date, chunk_hash) and not _needs_schema_refresh(
            reviews,
            page_id,
            review_date,
            chunk,
        ):
            skipped_count += 1
            continue

        review = parse_review(chunk, page_id, page_title, parser_warnings)
        if review is None:
            continue

        previous_count = len(reviews)
        reviews = [
            item
            for item in reviews
            if not (
                item.source_page_id == page_id
                and item.date == review.date
                and getattr(item, "review_type", "normal") == getattr(review, "review_type", "normal")
            )
        ]
        if len(reviews) < previous_count:
            updated_count += previous_count - len(reviews)
        else:
            added_count += 1
        reviews.append(review)
        update_review_state(state, review)

    return {
        "reviews": reviews,
        "added_count": added_count,
        "updated_count": updated_count,
        "skipped_count": skipped_count,
    }


def _active_page_ids(settings, state, messages: list[str]) -> list[str]:
    if settings.notion_active_page_ids:
        return settings.notion_active_page_ids
    if settings.active_months and settings.notion_page_ids:
        active_months = set(settings.active_months)
        matched_page_ids = [
            page_id
            for page_id in settings.notion_page_ids
            if _month_from_page_state(state, page_id) in active_months
        ]
        if matched_page_ids:
            return matched_page_ids
        messages.append(
            "ACTIVE_MONTHS が設定されていますが、stateからactive月のpage_idを特定できませんでした。"
            " NOTION_ACTIVE_PAGE_IDS を設定してください。"
        )
        return []
    return settings.notion_page_ids


def _month_from_page_state(state, page_id: str) -> str:
    page_state = state.pages.get(page_id)
    if not page_state:
        return ""
    title_match = re.search(r"(20\d{2})[-_/年. ]?(0?[1-9]|1[0-2])", page_state.title)
    if not title_match:
        return ""
    return f"{title_match.group(1)}-{int(title_match.group(2)):02d}"


def _review_date_from_chunk(chunk: str) -> str:
    date_line = re.search(r"(?m)^-\s*Date:\s*(.+?)\s*$", chunk)
    if date_line:
        parsed = parse_date(date_line.group(1))
        if parsed:
            return parsed.isoformat()
    header = re.search(
        r"(?im)^#\s+(\d{4}[-/]\d{1,2}[-/]\d{1,2})\s+(?:line\s+english\s+review|review)\b",
        chunk,
    )
    if header:
        parsed = parse_date(header.group(1).replace("/", "-"))
        if parsed:
            return parsed.isoformat()
    return ""


def _needs_schema_refresh(reviews: list[Review], page_id: str, review_date: str, chunk: str) -> bool:
    if "Words and phrases actually used" not in chunk:
        return False
    for review in reviews:
        if review.source_page_id == page_id and review.date.isoformat() == review_date:
            return not bool(getattr(review, "words_and_phrases_actually_used", []) or [])
    return False


def _month_from_title_or_reviews(title: str, reviews: list[Review], page_id: str) -> str:
    title_match = re.search(r"(20\d{2})[-_/年. ]?(0?[1-9]|1[0-2])", title)
    if title_match:
        return f"{title_match.group(1)}-{int(title_match.group(2)):02d}"
    page_reviews = [review for review in reviews if review.source_page_id == page_id]
    if page_reviews:
        return page_reviews[-1].date.strftime("%Y-%m")
    return ""


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
