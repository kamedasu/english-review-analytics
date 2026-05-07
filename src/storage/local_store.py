from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter

from src.config import DATA_DIR
from src.models import Review


REVIEWS_PATH = DATA_DIR / "processed" / "reviews.json"


def save_reviews(reviews: list[Review], path: Path = REVIEWS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [review.model_dump(mode="json") for review in reviews]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_reviews(path: Path = REVIEWS_PATH) -> list[Review]:
    if not path.exists():
        return []
    adapter = TypeAdapter(list[Review])
    return adapter.validate_json(path.read_text(encoding="utf-8"))
