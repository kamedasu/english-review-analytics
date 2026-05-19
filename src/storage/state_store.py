from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.config import DATA_DIR
from src.models import FetchState, PageState, Review, ReviewState


def state_path() -> Path:
    return DATA_DIR / "state" / "state.json"


def load_state(path: Path | None = None) -> FetchState:
    path = path or state_path()
    if not path.exists():
        return FetchState()
    return FetchState.model_validate_json(path.read_text(encoding="utf-8"))


def save_state(state: FetchState, path: Path | None = None) -> None:
    path = path or state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")


def should_process_page(state: FetchState, page_id: str, last_edited_time: str | None, page_hash: str) -> bool:
    previous = state.pages.get(page_id)
    if previous is None:
        return True
    previous_last_edited = _normalize_datetime(previous.last_edited_time)
    current_last_edited = _normalize_datetime(last_edited_time)
    return previous_last_edited != current_last_edited or previous.content_hash != page_hash


def update_page_state(
    state: FetchState,
    page_id: str,
    title: str,
    last_edited_time: str | None,
    page_hash: str,
) -> FetchState:
    state.pages[page_id] = PageState(
        page_id=page_id,
        title=title,
        last_edited_time=datetime.fromisoformat(last_edited_time.replace("Z", "+00:00"))
        if last_edited_time
        else None,
        content_hash=page_hash,
        last_fetched_at=datetime.now(timezone.utc),
    )
    return state


def review_state_key(source_page_id: str, review_date: str, review_hash: str) -> str:
    return f"{source_page_id}:{review_date}:{review_hash}"


def has_processed_review(state: FetchState, source_page_id: str, review_date: str, review_hash: str) -> bool:
    return review_state_key(source_page_id, review_date, review_hash) in state.reviews


def update_review_state(state: FetchState, review: Review) -> FetchState:
    key = review_state_key(review.source_page_id, review.date.isoformat(), review.content_hash)
    state.reviews[key] = ReviewState(
        review_id=review.review_id,
        source_page_id=review.source_page_id,
        source_page_title=review.source_page_title,
        date=review.date,
        content_hash=review.content_hash,
        updated_at=datetime.now(timezone.utc),
    )
    return state


def _normalize_datetime(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.astimezone(timezone.utc).isoformat()
