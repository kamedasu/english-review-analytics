from __future__ import annotations

from dataclasses import dataclass
import json

import requests

from src.config import get_settings
from src.models import Review, StudySummary


OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"


@dataclass(frozen=True)
class MonthlySummaryResult:
    text: str
    source: str
    model: str = ""
    warning: str = ""


def generate_rule_based_summary(summary: StudySummary, reviews: list[Review], period_type: str = "Monthly") -> str:
    if not reviews:
        return "この期間のレビューはまだありません。"

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

    period_label = _period_label_ja(period_type)
    return (
        f"{summary.month} の{period_label}では {summary.study_days} 日学習し、合計 "
        f"{summary.total_duration_minutes} 分取り組みました。"
        f"レビューは {summary.review_count} 件、フレーズは {summary.phrase_count} 件です。"
        f"最長連続学習日数は {summary.longest_streak} 日でした。"
        f"{priority_note}"
        f" 最近の主なトピック: {', '.join(topics[:5]) if topics else '未入力'}。"
    )


def generate_period_summary(
    summary: StudySummary,
    reviews: list[Review],
    period_type: str = "Monthly",
) -> MonthlySummaryResult:
    settings = get_settings()
    fallback_text = generate_rule_based_summary(summary, reviews, period_type)

    if not settings.openai_api_key:
        return MonthlySummaryResult(
            text=fallback_text,
            source="rule-based",
            warning="OPENAI_API_KEY が設定されていないため、rule-based summaryを表示しています。",
        )

    try:
        text = _generate_openai_summary(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            summary=summary,
            reviews=reviews,
            period_type=period_type,
        )
    except (requests.RequestException, ValueError, KeyError) as exc:
        return MonthlySummaryResult(
            text=fallback_text,
            source="rule-based",
            model=settings.openai_model,
            warning=f"OpenAI APIによるサマリー生成に失敗したため、rule-based summaryを表示しています: {exc}",
        )

    return MonthlySummaryResult(text=text, source="llm", model=settings.openai_model)


def generate_monthly_summary(summary: StudySummary, reviews: list[Review]) -> MonthlySummaryResult:
    return generate_period_summary(summary, reviews, "Monthly")


def generate_llm_summary_placeholder(summary: StudySummary, reviews: list[Review]) -> str:
    return generate_period_summary(summary, reviews).text


def _generate_openai_summary(
    api_key: str,
    model: str,
    summary: StudySummary,
    reviews: list[Review],
    period_type: str,
) -> str:
    response = requests.post(
        OPENAI_RESPONSES_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "instructions": _summary_instructions(period_type),
            "input": _build_summary_prompt(summary, reviews, period_type),
            "max_output_tokens": 900,
        },
        timeout=60,
    )
    if response.status_code >= 400:
        raise ValueError(f"{response.status_code}: {response.text[:500]}")

    data = response.json()
    text = _extract_response_text(data)
    if not text:
        raise ValueError("OpenAI response did not contain output text.")
    return text.strip()


def _summary_instructions(period_type: str) -> str:
    return (
        "あなたは英会話学習のコーチです。"
        f"学習レビューの構造化データを読み、日本語で具体的かつ短めに{_period_label_ja(period_type)}サマリーを書いてください。"
        "断定しすぎず、データから読み取れる範囲で提案してください。"
    )


def _build_summary_prompt(summary: StudySummary, reviews: list[Review], period_type: str) -> str:
    payload = {
        "period_type": period_type,
        "period": summary.month,
        "metrics": {
            "total_duration_minutes": summary.total_duration_minutes,
            "study_days": summary.study_days,
            "longest_streak": summary.longest_streak,
            "review_count": summary.review_count,
            "phrase_count": summary.phrase_count,
            "reused_phrase_count": summary.reused_phrase_count,
        },
        "reviews": [_review_for_prompt(review) for review in sorted(reviews, key=lambda item: item.date)],
    }
    return (
        f"以下の英会話レビュー分析データから、{_period_label_ja(period_type)}サマリーを作成してください。\n"
        "必ず次の見出しを含めてください。\n"
        f"## {_period_prefix_ja(period_type)}の成長ポイント\n"
        f"## {_period_prefix_ja(period_type)}の弱点\n"
        "## 次に増やすべき表現\n"
        "## 次回以降の学習テーマ\n\n"
        "各見出しは2から4個の箇条書きにしてください。\n"
        "抽象論だけでなく、topicやphraseから分かる具体例を入れてください。\n\n"
        f"データ:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def _period_label_ja(period_type: str) -> str:
    return {
        "Monthly": "月次",
        "Quarterly": "四半期",
        "Yearly": "年次",
    }.get(period_type, "期間")


def _period_prefix_ja(period_type: str) -> str:
    return {
        "Monthly": "今月",
        "Quarterly": "この四半期",
        "Yearly": "今年",
    }.get(period_type, "この期間")


def _review_for_prompt(review: Review) -> dict:
    return {
        "date": review.date.isoformat(),
        "duration_minutes": review.duration_minutes,
        "topic": review.topic,
        "good_points": review.good_points,
        "expressions_to_add": review.expressions_to_add,
        "expressions_to_use_next_time": review.expressions_to_use_next_time,
        "weak_points": getattr(review, "weak_points", []) or [],
        "more_natural_expressions": [
            {
                "your_phrase": expression.your_phrase,
                "more_natural": expression.more_natural,
                "note": expression.note,
            }
            for expression in (getattr(review, "more_natural_expressions", []) or [])
        ],
        "comment": review.comment,
        "phrase_cards": [
            {
                "phrase": card.phrase,
                "meaning": card.meaning,
                "example": card.example,
                "priority": card.priority,
                "next_review_date": card.next_review_date.isoformat() if card.next_review_date else "",
            }
            for card in review.phrase_cards
        ],
    }


def _extract_response_text(data: dict) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"]

    parts: list[str] = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                parts.append(content["text"])
    return "\n".join(parts)
