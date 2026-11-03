from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import json
import re

from src.config import get_settings
from src.models import Review
from src.notion_client import NotionClient, NotionClientError
from src.notion_parser import parse_review, split_reviews
from src.storage import StorageError, get_storage
from src.storage.base import Storage
from src.storage.state_store import (
    has_processed_review,
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
    try:
        reviews = sorted(_load_reviews(get_storage()), key=lambda review: review.date)
    except (StorageError, ValueError) as exc:
        return _storage_error_result(exc)
    messages = [f"ローカル保存済みデータを表示しています: {len(reviews)} reviews"]
    if not reviews:
        messages.append("ローカル保存済みレビューがありません。必要に応じて Sync from Notion を実行してください。")
    return LoadResult(
        reviews=reviews,
        debug=LoadDebugInfo(messages=messages),
    )


def load_or_fetch_reviews(refresh: bool = False) -> LoadResult:
    if refresh:
        return sync_reviews_from_notion()
    return load_local_reviews()


def sync_reviews_from_notion() -> LoadResult:
    messages: list[str] = []
    settings = get_settings()
    try:
        storage = get_storage()
        state = _load_state(storage)
        cached_reviews = _load_reviews(storage)
    except (StorageError, ValueError) as exc:
        return _storage_error_result(exc, sync_requested=True)

    page_ids = list(dict.fromkeys(settings.notion_page_ids))
    if not page_ids:
        return LoadResult(
            reviews=sorted(cached_reviews, key=lambda review: review.date),
            debug=LoadDebugInfo(messages=["NOTION_PAGE_IDS が未指定のため、保存済みデータのみ表示します。"]),
        )

    pages = []
    try:
        client = NotionClient(settings.notion_api_key)
        months = {}
        for page_id in page_ids:
            page = client.fetch_page_as_markdown(page_id)
            month = _month_from_title(page.title)
            if month in months:
                raise ValueError(f"対象月 {month} が複数のページIDで指定されています。同期を中止しました。")
            months[month] = page_id
            pages.append(page)
    except (NotionClientError, ValueError) as exc:
        error = str(exc)
        return LoadResult(
            reviews=sorted(cached_reviews, key=lambda review: review.date),
            debug=LoadDebugInfo(
                sync_requested=True,
                page_statuses=[PageLoadStatus(page_id="", status="エラー", error=error)],
                messages=[error],
            ),
        )

    # Backfill missing review keys only for selected pages; preserve historical state.
    for review in cached_reviews:
        if review.source_page_id in page_ids and not has_processed_review(
            state, review.source_page_id, review.date.isoformat(), review.content_hash
        ):
            update_review_state(state, review)
    all_reviews = cached_reviews
    page_statuses: list[PageLoadStatus] = []
    raw_writes = []

    for page in pages:
        page_hash = content_hash(page.markdown)
        raw_markdown_path = f"raw/{_safe_filename(page.title)}_{page.page_id}.md"
        raw_writes.append((raw_markdown_path, page.markdown))
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
                synced_month=_month_from_title(page.title),
                added_count=sync_result["added_count"],
                updated_count=sync_result["updated_count"],
                skipped_count=sync_result["skipped_count"],
            )
        )

    # Save state last: a failed raw/processed write must never mark a sync as complete.
    try:
        writes = raw_writes + [
            ("processed/reviews.json", json.dumps([review.model_dump(mode="json") for review in all_reviews], ensure_ascii=False, indent=2)),
            ("state/state.json", json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2)),
        ]
        _save_sync(storage, writes)
    except StorageError as exc:
        messages.append(str(exc))
        page_statuses.append(PageLoadStatus(page_id="", status="エラー", error=str(exc)))
        all_reviews = cached_reviews

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


def _month_from_title(title: str) -> str:
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}", title):
        try:
            date(int(title[:4]), int(title[5:]), 1)
            return title
        except ValueError:
            pass
    raise ValueError(f"Notionページタイトル {title!r} は有効な YYYY-MM 形式ではありません。同期を中止しました。")


def _save_sync(storage: Storage, writes: list[tuple[str, str]]) -> None:
    # Read backups before any writes. State is committed last on both backends.
    backups = {path: storage.load_text(path) if storage.exists(path) else None for path, _ in writes}
    attempted = []
    try:
        for path, content in writes:
            attempted.append(path)
            storage.save_text(path, content)
    except StorageError as exc:
        rollback_errors = []
        for path in reversed(attempted):
            try:
                if backups[path] is None:
                    storage.delete(path)
                else:
                    storage.save_text(path, backups[path])
            except StorageError:
                rollback_errors.append(path)
        if rollback_errors:
            raise StorageError(f"同期の保存に失敗し、復元できなかったファイルがあります: {', '.join(rollback_errors)}") from exc
        raise


def _load_reviews(storage: Storage) -> list[Review]:
    path = "processed/reviews.json"
    if not storage.exists(path):
        return []
    return [Review.model_validate(item) for item in storage.load_json(path)]


def _save_reviews(storage: Storage, reviews: list[Review]) -> None:
    storage.save_json("processed/reviews.json", [review.model_dump(mode="json") for review in reviews])


def _load_state(storage: Storage):
    from src.models import FetchState

    path = "state/state.json"
    if not storage.exists(path):
        return FetchState()
    return FetchState.model_validate(storage.load_json(path))


def _save_state(storage: Storage, state) -> None:
    storage.save_json("state/state.json", state.model_dump(mode="json"))


def _storage_error_result(exc: Exception, sync_requested: bool = False) -> LoadResult:
    message = f"保存先を読み込めませんでした: {exc}"
    return LoadResult(
        reviews=[],
        debug=LoadDebugInfo(
            sync_requested=sync_requested,
            page_statuses=[PageLoadStatus(page_id="", status="エラー", error=message)],
            messages=[message],
        ),
    )


def _safe_filename(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "_", value.strip())
    return normalized.strip("_")[:80] or "notion_page"


def _count_reviews_for_page(reviews: list[Review], page_id: str) -> int:
    return sum(1 for review in reviews if review.source_page_id == page_id)
