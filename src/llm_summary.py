from __future__ import annotations

from src.models import Review, StudySummary


def generate_rule_based_summary(summary: StudySummary, reviews: list[Review]) -> str:
    if not reviews:
        return "この月のレビューはまだありません。"

    topics = [review.topic for review in reviews if review.topic]
    frequent_priorities = [
        card.priority
        for review in reviews
        for card in review.phrase_cards
        if card.priority
    ]
    priority_note = ""
    if frequent_priorities:
        priority_note = f" 優先度つきフレーズは {len(frequent_priorities)} 件あります。"

    return (
        f"{summary.month} は {summary.study_days} 日学習し、合計 "
        f"{summary.total_duration_minutes} 分取り組みました。"
        f"レビューは {summary.review_count} 件、新規フレーズは {summary.phrase_count} 件です。"
        f"最長連続学習日数は {summary.longest_streak} 日でした。"
        f"{priority_note}"
        f" 最近の主なトピック: {', '.join(topics[:5]) if topics else '未入力'}。"
    )


def generate_llm_summary_placeholder(summary: StudySummary, reviews: list[Review]) -> str:
    return generate_rule_based_summary(summary, reviews)
