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
from src.storage.state_store import update_page_state
from src.review_identity import sync_review_blocks
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
    held_count: int = 0


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
    client = None
    try:
        client = NotionClient(settings.notion_api_key, on_retry=messages.append)
        months = {}
        seen_sources = set()
        for page_id in page_ids:
            page = client.fetch_page_as_markdown(page_id)
            month = _month_from_title(page.title)
            if month in months:
                raise ValueError(f"対象月 {month} が複数のページIDで指定されています。同期を中止しました。")
            months[month] = page_id
            if [block.markdown for block in page.review_blocks] != split_reviews(page.markdown):
                raise ValueError("Markdownと見出しブロックの対応が不一致です。同期を中止しました。")
            for block in page.review_blocks:
                if block.source_block_id and block.source_block_id in seen_sources:
                    raise ValueError("見出しブロックIDが重複しています。同期を中止しました。")
                if block.source_block_id:
                    seen_sources.add(block.source_block_id)
            pages.append(page)
    except (NotionClientError, ValueError) as exc:
        error = str(exc)
        return LoadResult(
            reviews=sorted(cached_reviews, key=lambda review: review.date),
            debug=LoadDebugInfo(
                sync_requested=True,
                page_statuses=[PageLoadStatus(page_id="", status="エラー", error=error)],
                messages=[*messages, error],
            ),
        )
    finally:
        if client is not None:
            client.close()

    all_reviews = cached_reviews
    page_statuses: list[PageLoadStatus] = []
    raw_writes = []

    for page in pages:
        page_hash = content_hash(page.markdown)
        raw_markdown_path = f"raw/{_safe_filename(page.title)}_{page.page_id}.md"
        raw_writes.append((raw_markdown_path, page.markdown))
        parser_warnings: list[str] = []
        try:
            previous_page = state.pages.get(page.page_id)
            old_title = previous_page.title if previous_page else page.title
            old_path = f"raw/{_safe_filename(old_title)}_{page.page_id}.md"
            historical_hashes = {
                content_hash(chunk) for chunk in split_reviews(storage.load_text(old_path))
            } if storage.exists(old_path) else set()
            sync_result = sync_review_blocks(
                page.page_id, page.title, page.review_blocks, all_reviews, state, parser_warnings,
                historical_hashes=historical_hashes, parser=parse_review,
            )
        except (StorageError, ValueError) as exc:
            return LoadResult(
                reviews=sorted(cached_reviews, key=lambda review: review.date),
                debug=LoadDebugInfo(sync_requested=True,
                    page_statuses=[PageLoadStatus(page_id=page.page_id, status="エラー", error=str(exc))],
                    messages=[str(exc)]),
            )
        all_reviews = sync_result.reviews
        raw_writes.append((f"raw/{_safe_filename(page.title)}_{page.page_id}.identity.json", json.dumps({
            "identity_version": 2, "page_id": page.page_id, "page_hash": page_hash,
            "reviews": [{"source_block_id": block.source_block_id, "content_hash": content_hash(block.markdown)} for block in page.review_blocks],
            "held": sync_result.held,
        }, ensure_ascii=False, indent=2)))
        update_page_state(state, page.page_id, page.title, page.last_edited_time, page_hash)
        messages.append(
            f"{page.title}: added {sync_result.added_count}, "
            f"updated {sync_result.updated_count}, skipped {sync_result.skipped_count}, held {len(sync_result.held)}"
        )
        messages.extend(parser_warnings)
        page_statuses.append(
            PageLoadStatus(
                page_id=page.page_id,
                title=page.title,
                status="保留あり" if sync_result.held else "同期済み",
                review_count=_count_reviews_for_page(all_reviews, page.page_id),
                last_edited_time=page.last_edited_time,
                raw_markdown_path=str(raw_markdown_path),
                parser_warnings=parser_warnings,
                synced_month=_month_from_title(page.title),
                added_count=sync_result.added_count,
                updated_count=sync_result.updated_count,
                skipped_count=sync_result.skipped_count,
                held_count=len(sync_result.held),
            )
        )

    # Save state last: a failed raw/processed write must never mark a sync as complete.
    try:
        writes = raw_writes + [
            ("processed/reviews.json", json.dumps([
                review.model_dump(mode="json", exclude={"source_block_id"} if not review.source_block_id else set())
                for review in all_reviews
            ], ensure_ascii=False, indent=2)),
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
