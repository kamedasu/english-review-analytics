from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


class PhraseCard(BaseModel):
    phrase: str = ""
    meaning: str = ""
    example: str = ""
    next_review_date: date | None = None
    priority: str = ""
    source: str = ""
    review_status: str = ""
    source_review_id: str = ""
    source_review_date: date | None = None


class MoreNaturalExpression(BaseModel):
    your_phrase: str = ""
    more_natural: str = ""
    note: str = ""
    source_review_id: str = ""
    source_review_date: date | None = None


class Review(BaseModel):
    review_id: str
    source_page_id: str = ""
    source_page_title: str = ""
    review_type: str = "normal"
    date: date
    duration_minutes: int = 0
    topic: str = ""
    situation: str | None = None
    my_draft: list[str] = Field(default_factory=list)
    more_natural_version: list[str] = Field(default_factory=list)
    why_it_was_corrected: list[str] = Field(default_factory=list)
    good_points: list[str] = Field(default_factory=list)
    expressions_to_add: list[str] = Field(default_factory=list)
    expressions_to_use_next_time: list[str] = Field(default_factory=list)
    weak_points: list[str] = Field(default_factory=list)
    more_natural_expressions: list[MoreNaturalExpression] = Field(default_factory=list)
    words_and_phrases_actually_used: list[str] = Field(default_factory=list)
    comment: str = ""
    phrase_cards: list[PhraseCard] = Field(default_factory=list)
    raw_markdown: str = ""
    content_hash: str = ""


class StudySummary(BaseModel):
    month: str
    total_duration_minutes: int
    study_days: int
    longest_streak: int
    review_count: int
    phrase_count: int
    reused_phrase_count: int = 0
    llm_summary: str | None = None


class PageState(BaseModel):
    page_id: str
    title: str = ""
    last_edited_time: datetime | None = None
    content_hash: str = ""
    last_fetched_at: datetime | None = None


class ReviewState(BaseModel):
    review_id: str
    source_page_id: str
    source_page_title: str = ""
    date: date
    content_hash: str
    updated_at: datetime | None = None


class FetchState(BaseModel):
    pages: dict[str, PageState] = Field(default_factory=dict)
    reviews: dict[str, ReviewState] = Field(default_factory=dict)
