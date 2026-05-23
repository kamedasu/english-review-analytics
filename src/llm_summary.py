from __future__ import annotations

from dataclasses import dataclass
import json
import time

import requests

from src.config import get_settings
from src.models import Review, StudySummary
from src.reuse_detector import ReviewTargetSummary, normalize_phrase, summarize_review_targets


OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"


@dataclass(frozen=True)
class MonthlySummaryResult:
    text: str
    source: str
    model: str = ""
    warning: str = ""


class RetryableOpenAIError(ValueError):
    def __init__(self, status_code: int, message: str):
        super().__init__(f"{status_code}: {message}")
        self.status_code = status_code


def generate_rule_based_summary(summary: StudySummary, reviews: list[Review], period_type: str = "Monthly") -> str:
    if not reviews:
        return "この期間のレビューはまだありません。"

    topics = [review.topic for review in reviews if review.topic]
    period_label = _period_label_ja(period_type)
    period_prefix = _period_prefix_ja(period_type)
    context = _summary_learning_context(reviews, period_type)
    top_reused = context["top_reused_phrases"]
    unused = context["unused_expression_candidates"]
    weak_points = context["weak_points"][:3]
    natural = context["more_natural_examples"][:3]

    reused_lines = _format_phrase_list(top_reused, "reused_count")
    unused_lines = _format_phrase_list(unused, "source")
    weak_lines = "\n".join(f"- {item}" for item in weak_points) if weak_points else "- 未記録"
    natural_lines = (
        "\n".join(
            f"- {item['your_phrase']} -> {item['more_natural']}"
            + (f" ({item['note']})" if item.get("note") else "")
            for item in natural
        )
        if natural
        else "- 未記録"
    )

    return (
        f"## {period_prefix}の成長ポイント\n"
        f"- {period_label}では {summary.study_days} 日、合計 {summary.total_duration_minutes} 分学習しました。\n"
        f"- 主な会話テーマ: {', '.join(topics[:5]) if topics else '未入力'}。\n"
        "- よく再利用できた表現:\n"
        f"{reused_lines}\n\n"
        f"## {period_prefix}の弱点\n"
        "- 記録されたweak points:\n"
        f"{weak_lines}\n"
        "- より自然な言い換えの例:\n"
        f"{natural_lines}\n\n"
        "## 次に増やすべき表現\n"
        f"{unused_lines}\n"
        "- 各表現は次回の会話で1回ずつ使うことを目標にしてください。\n\n"
        "## 次回以降の学習テーマ\n"
        "- 未使用表現から1つ選び、日常・食べ物・旅行・仕事後の話題で使う。\n"
        "- More natural expressions に出た言い換えを、短い例文で言い直す。\n\n"
        f"## {period_prefix}の再利用成功フレーズ Top 5\n"
        f"{reused_lines}\n\n"
        "## まだ使えていない表現 Top 5\n"
        f"{unused_lines}\n\n"
        "## 1分復習ドリル\n"
        "- 日本語→英語: 今日の気分を、未使用表現を1つ使って言う。\n"
        "- 穴埋め: I want to ____ after work.\n"
        "- 言い換え: more natural expressions の1つを使って短く言い直す。"
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
            retry_count=settings.openai_summary_retry_count,
        )
    except RetryableOpenAIError as exc:
        retry_count = max(settings.openai_summary_retry_count, 0)
        return MonthlySummaryResult(
            text=fallback_text,
            source="rule-based",
            model=settings.openai_model,
            warning=(
                "OpenAI APIの一時的なserver_errorが続いたため、rule-based summaryにfallbackしました "
                f"(last_status={exc.status_code}, retries={retry_count})。"
            ),
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
    retry_count: int,
) -> str:
    retry_count = max(retry_count, 0)
    response = None
    for attempt in range(retry_count + 1):
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
                "max_output_tokens": 2200,
            },
            timeout=60,
        )
        if response.status_code not in {500, 502, 503, 504}:
            break
        if attempt >= retry_count:
            raise RetryableOpenAIError(response.status_code, response.text[:500])
        time.sleep(2**attempt)

    if response is None:
        raise ValueError("OpenAI response was not created.")
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
        f"学習レビューの構造化データを読み、日本語で具体的で実践的な{_period_label_ja(period_type)}サマリーを書いてください。"
        "断定しすぎず、データから読み取れる範囲で提案してください。"
        "危険、不自然、文脈リスクが高い表現は避け、日常会話で安全に使いやすい表現を優先してください。"
    )


def _build_summary_prompt(summary: StudySummary, reviews: list[Review], period_type: str) -> str:
    learning_context = _summary_learning_context(reviews, period_type)
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
        "learning_context": learning_context,
    }
    period_prefix = _period_prefix_ja(period_type)
    detail_note = (
        "Monthlyは具体例をやや詳しめにしてください。"
        if period_type == "Monthly"
        else "Quarterly / Yearlyは要点を絞ってMonthlyより簡潔にしてください。"
    )
    return (
        f"以下の英会話レビュー分析データから、{_period_label_ja(period_type)}サマリーを作成してください。\n"
        "単なる総評ではなく、次回の会話で何を言うか、何を復習するかが分かる形にしてください。\n"
        f"{detail_note}\n"
        "必ず次の見出しをこの順番で含めてください。\n"
        f"## {period_prefix}の成長ポイント\n"
        f"## {period_prefix}の弱点\n"
        "## 次に増やすべき表現\n"
        "## 次回以降の学習テーマ\n"
        f"## {period_prefix}の再利用成功フレーズ Top 5\n"
        "## まだ使えていない表現 Top 5\n"
        "## 1分復習ドリル\n\n"
        "入力データは全文レビューではなく、summary生成用に要約済みのコンテキストです。\n"
        "各見出しの要件:\n"
        f"- {period_prefix}の成長ポイント: 今期よく使えた表現、強くなった会話スキル、成長を示す具体例を含める。\n"
        "- 弱点: 弱点名、よくある誤り、より自然な形、短い例文を各3つ程度含める。\n"
        "- 次に増やすべき表現: phrase、意味、使う場面、短い例文3つを含める。日常会話で使いやすく誤用リスクが低いものを優先する。\n"
        "- 次回以降の学習テーマ: 抽象的なテーマではなく、次回会話で実行するミッション形式にする。\n"
        "- 再利用成功フレーズ Top 5: learning_context.top_reused_phrases を優先し、reused_countを添える。\n"
        "- まだ使えていない表現 Top 5: learning_context.unused_expression_candidates を優先する。\n"
        "- 1分復習ドリル: 日本語→英語、穴埋め、言い換えを合計3問だけ出す。\n"
        "全体は箇条書き中心で、長くなりすぎないようにしてください。\n\n"
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


def _summary_learning_context(reviews: list[Review], period_type: str) -> dict:
    limit = 5 if period_type == "Monthly" else 3
    example_limit = 2 if period_type == "Monthly" else 1
    retention = summarize_review_targets(reviews, reviews)
    top_reused = [
        _retention_item_for_prompt(item, example_limit)
        for item in retention
        if item.reused_count > 0
    ][:5]
    unused = [
        _retention_item_for_prompt(item, example_limit)
        for item in _unused_expression_candidates(retention)
    ][:5]

    topics = _top_normalized_values(review.topic for review in reviews)
    actual_used = _top_normalized_values(
        phrase
        for review in reviews
        for phrase in (getattr(review, "words_and_phrases_actually_used", []) or [])
    )
    weak_points = [
        value
        for value in _top_normalized_values(
            weak_point
            for review in reviews
            for weak_point in (getattr(review, "weak_points", []) or [])
        )
    ][:limit]
    natural_examples = [
        {
            "your_phrase": expression.your_phrase,
            "more_natural": expression.more_natural,
            "note": expression.note,
        }
        for review in sorted(reviews, key=lambda item: item.date, reverse=True)
        for expression in (getattr(review, "more_natural_expressions", []) or [])
    ][:limit]

    return {
        "top_reused_phrases": top_reused,
        "unused_expression_candidates": unused,
        "weak_points": weak_points,
        "more_natural_examples": natural_examples,
        "topics": topics[:5],
        "actually_used_top": actual_used[:5],
        "context_limits": {
            "top_reused_phrases": 5,
            "unused_expression_candidates": 5,
            "weak_points": limit,
            "more_natural_examples": limit,
            "topics": 5,
        },
    }


def _retention_item_for_prompt(item: ReviewTargetSummary, example_limit: int) -> dict:
    data = {
        "phrase": item.phrase,
        "reused_count": item.reused_count,
        "review_status": item.review_status,
    }
    if item.highest_priority:
        data["highest_priority"] = item.highest_priority
    if item.meanings:
        data["meanings"] = item.meanings[:example_limit]
    if item.examples:
        data["examples"] = item.examples[:example_limit]
    return data


def _unused_expression_candidates(retention: list[ReviewTargetSummary]) -> list[ReviewTargetSummary]:
    priority_rank = {"High": 3, "Medium": 2, "Low": 1}
    candidates = [
        item
        for item in retention
        if "actually_used" not in item.matched_fields
    ]
    return sorted(
        candidates,
        key=lambda item: (
            priority_rank.get(item.highest_priority, 0),
            item.first_seen_date,
            item.phrase.lower(),
        ),
        reverse=True,
    )


def _top_normalized_values(values) -> list[str]:
    grouped: dict[str, dict] = {}
    for value in values:
        text = (value or "").strip()
        key = normalize_phrase(text)
        if not key:
            continue
        if key not in grouped:
            grouped[key] = {"text": text, "count": 0}
        grouped[key]["count"] += 1
    return [
        item["text"]
        for item in sorted(grouped.values(), key=lambda item: (item["count"], item["text"].lower()), reverse=True)
    ]


def _format_phrase_list(items: list[dict], detail_key: str) -> str:
    if not items:
        return "- 該当なし"
    lines = []
    for item in items[:5]:
        detail = item.get(detail_key, "")
        suffix = f" ({detail_key}: {detail})" if detail != "" else ""
        meaning = ""
        if item.get("meanings"):
            meaning = f" - {item['meanings'][0]}"
        lines.append(f"- {item.get('phrase', '')}{meaning}{suffix}")
    return "\n".join(lines)


def _extract_response_text(data: dict) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"]

    parts: list[str] = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                parts.append(content["text"])
    return "\n".join(parts)
