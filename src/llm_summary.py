from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
import re
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

    period_label = _period_label_ja(period_type)
    period_prefix = _period_prefix_ja(period_type)
    context = _summary_learning_context(reviews, period_type)
    topics = context["topics"]

    growth_lines = _format_growth_lines(context["growth_highlights"], period_label)
    weakness_lines = _format_weakness_lines(context["weakness_highlights"])
    next_expression_lines = _format_next_expression_lines(context["next_expression_candidates"][:10])
    reused_lines = _format_phrase_list(context["top_reused_phrases"], "reused_count")
    unused_lines = _format_next_expression_lines(context["unused_expression_candidates"][:5], include_status=False)
    theme_lines = _format_learning_theme_lines(context["learning_themes"])
    drill_lines = _format_drill_lines(context["drill_items"])

    return (
        f"## {period_prefix}の成長ポイント\n"
        f"- {period_label}では {summary.study_days} 日、合計 {summary.total_duration_minutes} 分学習しました。\n"
        f"- 主な会話テーマ: {', '.join(topics[:5]) if topics else '未入力'}。\n"
        f"{growth_lines}\n\n"
        f"## {period_prefix}の弱点\n"
        f"{weakness_lines}\n\n"
        "## 次に増やすべき表現\n"
        f"{next_expression_lines}\n\n"
        "## 次回以降の学習テーマ\n"
        f"{theme_lines}\n\n"
        f"## {period_prefix}の再利用成功フレーズ Top 5\n"
        f"{reused_lines}\n\n"
        "## まだ使えていない表現 Top 5\n"
        f"{unused_lines}\n\n"
        "## 1分復習ドリル\n"
        f"{drill_lines}"
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
        f"- {period_prefix}の成長ポイント: good_points の繰り返し傾向と、Retained / Strong に寄っている phrase や繰り返し出てきた confirmed phrase を優先してまとめる。\n"
        "- 弱点: weak_points の繰り返し傾向、more_natural_expressions の修正傾向、confirmed で何度も出る未定着 phrase を統合してまとめる。\n"
        "- 次に増やすべき表現: phrase_cards から今の学習者にとって伸びしろが大きい未定着表現を10件まで選ぶ。Strong や十分定着済みの表現は外す。\n"
        "- 次回以降の学習テーマ: 抽象的なテーマではなく、次回会話で実行するミッション形式にする。\n"
        "- 再利用成功フレーズ Top 5: phrase_cards ベースで、confirmed / recommended、句動詞、コロケーション、複数語フレーズ、High / Medium priority を優先する。固有名詞や単純語は避ける。十分に良い候補が少ない場合は無理に5件埋めない。\n"
        "- まだ使えていない表現 Top 5: phrase_cards ベースで、学習価値の高い未定着表現を優先し、単純語や固有名詞は避ける。\n"
        "- 1分復習ドリル: 合計5問。日本語→英語2問、穴埋め2問、言い換え1問を基本にし、問題と回答を明確に分ける。\n"
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
    phrase_limit = 20 if period_type == "Monthly" else 12
    retention = summarize_review_targets(reviews, reviews)
    phrase_stats = _build_phrase_learning_stats(reviews, retention)
    good_points = _collect_text_patterns(
        good_point
        for review in reviews
        for good_point in (getattr(review, "good_points", []) or [])
    )
    weak_points = _collect_text_patterns(
        weak_point
        for review in reviews
        for weak_point in (getattr(review, "weak_points", []) or [])
    )
    natural_patterns = _collect_more_natural_patterns(reviews)
    topics = _top_normalized_values(review.topic for review in reviews)
    actual_used = _top_normalized_values(
        phrase
        for review in reviews
        for phrase in (getattr(review, "words_and_phrases_actually_used", []) or [])
    )
    next_expression_candidates = _next_expression_candidates(phrase_stats)
    top_reused = [
        _phrase_stat_for_prompt(item, example_limit)
        for item in _top_reused_phrase_stats(phrase_stats)[:5]
    ]
    unused = [
        _phrase_stat_for_prompt(item, example_limit)
        for item in next_expression_candidates
        if "actually_used" not in item["matched_fields"]
    ][:5]

    return {
        "top_reused_phrases": top_reused,
        "unused_expression_candidates": unused,
        "good_points": good_points[:limit],
        "weak_points": weak_points[:limit],
        "more_natural_examples": natural_patterns[:limit],
        "growth_highlights": _growth_highlights(good_points, phrase_stats, limit, example_limit),
        "weakness_highlights": _weakness_highlights(weak_points, natural_patterns, phrase_stats, limit, example_limit),
        "next_expression_candidates": [_phrase_stat_for_prompt(item, example_limit) for item in next_expression_candidates[:10]],
        "learning_themes": _learning_themes(weak_points, natural_patterns, next_expression_candidates),
        "drill_items": _build_drill_items(natural_patterns, next_expression_candidates, phrase_stats),
        "phrase_learning_stats": [_phrase_stat_for_prompt(item, example_limit) for item in phrase_stats[:phrase_limit]],
        "line_review_examples": _line_review_examples(reviews, limit),
        "topics": topics[:5],
        "actually_used_top": actual_used[:5],
        "context_limits": {
            "top_reused_phrases": 5,
            "unused_expression_candidates": 5,
            "next_expression_candidates": 10,
            "weak_points": limit,
            "more_natural_examples": limit,
            "phrase_learning_stats": phrase_limit,
            "topics": 5,
        },
    }


def _line_review_examples(reviews: list[Review], limit: int) -> list[dict]:
    examples: list[dict] = []
    for review in sorted(reviews, key=lambda item: item.date, reverse=True):
        if getattr(review, "review_type", "normal") != "line":
            continue
        examples.append(
            {
                "date": review.date.isoformat(),
                "topic": review.topic,
                "situation": review.situation or "",
                "my_draft": (getattr(review, "my_draft", []) or [])[:2],
                "more_natural_version": (getattr(review, "more_natural_version", []) or [])[:2],
                "why_it_was_corrected": (getattr(review, "why_it_was_corrected", []) or [])[:2],
            }
        )
        if len(examples) >= limit:
            break
    return examples


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


def _phrase_stat_for_prompt(item: dict, example_limit: int) -> dict:
    data = {
        "phrase": item["phrase"],
        "display_phrase": item["display_phrase"],
        "occurrence_count": item["occurrence_count"],
        "reused_count": item["reused_count"],
        "review_status": item["review_status"],
        "source": item["primary_source"],
        "source_counts": item["source_counts"],
        "status_counts": item["status_counts"],
        "highest_priority": item["highest_priority"],
        "is_learning_target": item["is_learning_target"],
        "learning_value_score": item["learning_value_score"],
    }
    if item["meanings"]:
        data["meanings"] = item["meanings"][:example_limit]
    if item["examples"]:
        data["examples"] = item["examples"][:example_limit]
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


def _build_phrase_learning_stats(reviews: list[Review], retention: list[ReviewTargetSummary]) -> list[dict]:
    grouped: dict[str, dict] = {}
    retention_map = {normalize_phrase(item.phrase): item for item in retention}

    for review in sorted(reviews, key=lambda item: item.date):
        for card in getattr(review, "phrase_cards", []) or []:
            key = normalize_phrase(card.phrase)
            if not key:
                continue
            if key not in grouped:
                grouped[key] = {
                    "phrase": card.phrase,
                    "occurrence_count": 0,
                    "source_counts": Counter(),
                    "status_counts": Counter(),
                    "meanings": set(),
                    "examples": set(),
                    "highest_priority": "",
                }
            item = grouped[key]
            item["occurrence_count"] += 1
            source = _normalize_source(card.source)
            if source:
                item["source_counts"][source] += 1
            status = _normalize_review_status(card.review_status)
            if status:
                item["status_counts"][status] += 1
            item["highest_priority"] = _higher_priority(item["highest_priority"], card.priority)
            if card.meaning:
                item["meanings"].add(card.meaning)
            if card.example:
                item["examples"].add(card.example)

    results: list[dict] = []
    for key, item in grouped.items():
        retention_item = retention_map.get(key)
        primary_source = _primary_counter_value(item["source_counts"])
        explicit_status = _primary_counter_value(item["status_counts"])
        inferred_status = retention_item.review_status if retention_item else ""
        resolved_status = explicit_status or inferred_status or "New"
        status_rank = _review_status_rank(resolved_status)
        learning_value_score = _learning_value_score(
            phrase=item["phrase"],
            primary_source=primary_source,
            highest_priority=item["highest_priority"],
            resolved_status=resolved_status,
            occurrence_count=item["occurrence_count"],
        )
        is_learning_target = status_rank < _review_status_rank("Strong") and learning_value_score > 0
        results.append(
            {
                "phrase": item["phrase"],
                "display_phrase": _core_expression_display(item["phrase"]),
                "occurrence_count": item["occurrence_count"],
                "source_counts": dict(item["source_counts"]),
                "status_counts": dict(item["status_counts"]),
                "primary_source": primary_source,
                "highest_priority": item["highest_priority"],
                "meanings": sorted(item["meanings"]),
                "examples": sorted(item["examples"]),
                "reused_count": retention_item.reused_count if retention_item else 0,
                "review_status": resolved_status,
                "retention_status": inferred_status or resolved_status,
                "matched_fields": retention_item.matched_fields if retention_item else [],
                "is_learning_target": is_learning_target,
                "learning_value_score": learning_value_score,
                "needs_review": _needs_review(item, retention_item, resolved_status),
            }
        )

    priority_rank = {"High": 3, "Medium": 2, "Low": 1}
    return sorted(
        results,
        key=lambda item: (
            item["reused_count"],
            item["learning_value_score"],
            _review_status_rank(item["review_status"]),
            priority_rank.get(item["highest_priority"], 0),
            item["occurrence_count"],
            item["phrase"].lower(),
        ),
        reverse=True,
    )


def _collect_text_patterns(values) -> list[dict]:
    grouped: dict[str, dict] = {}
    for value in values:
        text = (value or "").strip()
        key = normalize_phrase(text)
        if not key:
            continue
        if key not in grouped:
            grouped[key] = {"text": text, "count": 0}
        grouped[key]["count"] += 1
    return sorted(grouped.values(), key=lambda item: (item["count"], item["text"].lower()), reverse=True)


def _collect_more_natural_patterns(reviews: list[Review]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for review in sorted(reviews, key=lambda item: item.date, reverse=True):
        for expression in (getattr(review, "more_natural_expressions", []) or []):
            label = _more_natural_pattern_label(expression)
            key = normalize_phrase(label)
            if not key:
                continue
            if key not in grouped:
                grouped[key] = {
                    "pattern": label,
                    "count": 0,
                    "examples": [],
                }
            grouped[key]["count"] += 1
            if len(grouped[key]["examples"]) < 2:
                grouped[key]["examples"].append(
                    {
                        "your_phrase": expression.your_phrase,
                        "more_natural": expression.more_natural,
                        "note": expression.note,
                    }
                )
    return sorted(grouped.values(), key=lambda item: (item["count"], item["pattern"].lower()), reverse=True)


def _growth_highlights(good_points: list[dict], phrase_stats: list[dict], limit: int, example_limit: int) -> list[dict]:
    highlights: list[dict] = []
    good_limit = max(limit - 2, 1)
    phrase_limit = max(limit - good_limit, 1)
    for item in good_points[:good_limit]:
        highlights.append(
            {
                "type": "good_point",
                "label": item["text"],
                "count": item["count"],
            }
        )
    stable_phrases = [
        item
        for item in phrase_stats
        if item["learning_value_score"] >= 4
        and (
            _review_status_rank(item["review_status"]) >= _review_status_rank("Retained")
            or (item["primary_source"] == "confirmed" and item["occurrence_count"] >= 2)
        )
    ]
    for item in stable_phrases[:phrase_limit]:
        highlights.append(
            {
                "type": "phrase",
                "label": item["display_phrase"],
                "count": item["occurrence_count"],
                "review_status": item["review_status"],
                "source": item["primary_source"],
                "meanings": item["meanings"][:example_limit],
                "skill_label": _growth_skill_label(item["display_phrase"], item["meanings"]),
            }
        )
    return highlights[:limit]


def _weakness_highlights(
    weak_points: list[dict],
    natural_patterns: list[dict],
    phrase_stats: list[dict],
    limit: int,
    example_limit: int,
) -> list[dict]:
    highlights: list[dict] = []
    for item in weak_points[:limit]:
        highlights.append({"type": "weak_point", "label": item["text"], "count": item["count"]})
    for item in natural_patterns[:limit]:
        highlights.append(
            {
                "type": "more_natural",
                "label": item["pattern"],
                "count": item["count"],
                "examples": item["examples"][:example_limit],
            }
        )
    repeated_confirmed = [
        item
        for item in phrase_stats
        if item["primary_source"] == "confirmed"
        and item["occurrence_count"] >= 2
        and _review_status_rank(item["review_status"]) < _review_status_rank("Retained")
    ]
    for item in repeated_confirmed[:limit]:
        highlights.append(
            {
                "type": "repeated_confirmed",
                "label": item["display_phrase"],
                "count": item["occurrence_count"],
                "review_status": item["review_status"],
                "meanings": item["meanings"][:example_limit],
            }
        )
    return highlights[: limit + 2]


def _next_expression_candidates(phrase_stats: list[dict]) -> list[dict]:
    priority_rank = {"High": 3, "Medium": 2, "Low": 1}
    candidates = [item for item in phrase_stats if item["is_learning_target"] and item["needs_review"]]
    return sorted(
        candidates,
        key=lambda item: (
            item["learning_value_score"],
            2 if item["primary_source"] == "confirmed" else 1 if item["primary_source"] == "recommended" else 0,
            priority_rank.get(item["highest_priority"], 0),
            1 if item["review_status"] == "New" else 0,
            item["occurrence_count"],
            item["phrase"].lower(),
        ),
        reverse=True,
    )


def _top_reused_phrase_stats(phrase_stats: list[dict]) -> list[dict]:
    candidates = [
        item
        for item in phrase_stats
        if item["learning_value_score"] > 0
        and (
            item["reused_count"] > 0 or _review_status_rank(item["review_status"]) >= _review_status_rank("Retained")
        )
    ]
    return sorted(
        candidates,
        key=lambda item: (
            item["learning_value_score"],
            item["reused_count"],
            _review_status_rank(item["review_status"]),
            item["occurrence_count"],
            item["phrase"].lower(),
        ),
        reverse=True,
    )


def _learning_themes(weak_points: list[dict], natural_patterns: list[dict], next_expression_candidates: list[dict]) -> list[str]:
    themes: list[str] = []
    if weak_points:
        themes.append(f"弱点の再発防止: 「{weak_points[0]['text']}」を次回会話で1回は意識して修正する。")
    if natural_patterns:
        themes.append(f"言い換え強化: 「{natural_patterns[0]['pattern']}」に関する修正を1つ選び、その場で言い直す。")
    if next_expression_candidates:
        phrase = next_expression_candidates[0]["display_phrase"]
        themes.append(f"新出表現の実戦投入: 「{phrase}」を日常トピックで1回自然に使う。")
    if not themes:
        themes.append("次回会話で新しい phrase を1つ選び、会話中に最低1回使う。")
    return themes[:3]


def _build_drill_items(natural_patterns: list[dict], next_expression_candidates: list[dict], phrase_stats: list[dict]) -> list[dict]:
    drills: list[dict] = []
    candidates = next_expression_candidates[:4]
    fallback_candidates = [item for item in phrase_stats if item["learning_value_score"] > 0][:4]
    source_items = candidates or fallback_candidates

    translation_templates = [
        "I'd like to work {phrase} into the conversation naturally.",
        "I'm trying to use {phrase} more smoothly when I speak.",
    ]
    for index, item in enumerate(source_items[:2]):
        phrase = item["display_phrase"]
        meaning = item["meanings"][0] if item["meanings"] else "短い一文で言える形から固定する。"
        drills.append(
            {
                "question": f"日本語→英語: 「次回の会話で {phrase} を自然に使いたい」を英語で言う。",
                "answer": translation_templates[index].format(phrase=phrase),
                "note": meaning,
            }
        )

    fill_items = source_items[2:4] if len(source_items) >= 4 else source_items[:2]
    for item in fill_items[:2]:
        phrase = item["display_phrase"]
        example = item["examples"][0] if item["examples"] else f"I want to {phrase} after work."
        sentence = example.replace(phrase, "____", 1) if phrase in example else f"I want to ____ using {phrase}."
        drills.append(
            {
                "question": f"穴埋め: {sentence}",
                "answer": phrase,
                "note": "フレーズをかたまりで入れる。",
            }
        )

    if natural_patterns:
        example = natural_patterns[0]["examples"][0] if natural_patterns[0]["examples"] else {}
        your_phrase = example.get("your_phrase", "My expression")
        more_natural = example.get("more_natural", "A more natural version")
        note = example.get("note", natural_patterns[0]["pattern"])
        drills.append(
            {
                "question": f"言い換え: 「{your_phrase}」をもっと自然に言い直す。",
                "answer": more_natural,
                "note": note,
            }
        )
    else:
        fallback = source_items[0]["display_phrase"] if source_items else "set aside"
        drills.append(
            {
                "question": f"言い換え: 「I want to use {fallback}.」を、より自然で会話的な一文に言い換える。",
                "answer": f"I'd like to work {fallback} into the conversation naturally.",
                "note": "少し会話的なトーンにする。",
            }
        )

    while len(drills) < 5:
        fallback = source_items[0]["display_phrase"] if source_items else "set aside"
        drills.append(
            {
                "question": f"穴埋め: I'm trying to ____ this phrase in conversation.",
                "answer": fallback,
                "note": "未定着表現を実戦で使う意識を持つ。",
            }
        )

    return drills[:5]


def _normalize_source(value: str) -> str:
    text = (value or "").strip().lower()
    if "confirm" in text:
        return "confirmed"
    if "recommend" in text:
        return "recommended"
    return text


def _normalize_review_status(value: str) -> str:
    text = (value or "").strip().lower()
    mapping = {
        "new": "New",
        "reused": "Reused",
        "retained": "Retained",
        "strong": "Strong",
    }
    return mapping.get(text, "")


def _review_status_rank(value: str) -> int:
    return {"New": 0, "Reused": 1, "Retained": 2, "Strong": 3}.get(value, 0)


def _primary_counter_value(counter: Counter) -> str:
    if not counter:
        return ""
    return sorted(counter.items(), key=lambda item: (item[1], item[0]), reverse=True)[0][0]


def _needs_review(item: dict, retention_item: ReviewTargetSummary | None, resolved_status: str) -> bool:
    status_rank = _review_status_rank(resolved_status)
    if status_rank >= _review_status_rank("Strong"):
        return False
    if status_rank >= _review_status_rank("Retained") and item["occurrence_count"] >= 3:
        return False
    if retention_item and "actually_used" not in retention_item.matched_fields:
        return True
    return item["occurrence_count"] <= 2


def _learning_value_score(
    phrase: str,
    primary_source: str,
    highest_priority: str,
    resolved_status: str,
    occurrence_count: int,
) -> int:
    score = 0
    text = (phrase or "").strip()
    normalized = normalize_phrase(text)
    if not normalized:
        return 0
    word_count = len([part for part in normalized.split(" ") if part])

    if primary_source == "confirmed":
        score += 4
    elif primary_source == "recommended":
        score += 3

    if highest_priority == "High":
        score += 3
    elif highest_priority == "Medium":
        score += 2

    if resolved_status in {"Reused", "Retained", "Strong"}:
        score += 2

    if word_count >= 2:
        score += 4
    elif re.search(r"\b(?:up|out|off|on|in|over|away|back|through|around)\b", normalized):
        score += 2
    else:
        score -= 2

    if occurrence_count >= 2:
        score += 1

    if _looks_like_low_value_phrase(text):
        score -= 8

    return score


def _looks_like_low_value_phrase(value: str) -> bool:
    text = (value or "").strip()
    normalized = normalize_phrase(text)
    if not normalized:
        return True
    parts = [part for part in normalized.split(" ") if part]
    if len(parts) == 1:
        token = parts[0]
        if token in {
            "dinner",
            "lunch",
            "breakfast",
            "tokyo",
            "yoga",
            "japan",
            "cafe",
            "coffee",
            "work",
            "school",
            "food",
            "movie",
            "music",
            "friend",
            "family",
            "weekend",
            "today",
            "tomorrow",
        }:
            return True
        if len(token) <= 3:
            return True
    if len(parts) == 2 and parts[0] in {"the", "a", "an"} and parts[1] in {
        "fans",
        "audience",
        "movie",
        "music",
        "food",
        "friend",
        "family",
        "coffee",
        "dinner",
        "lunch",
    }:
        return True
    if re.fullmatch(r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*", text):
        return True
    if re.fullmatch(r"[A-Z]{2,}(?:\s+[A-Z]{2,})*", text):
        return True
    return False


def _core_expression_display(value: str) -> str:
    text = " ".join((value or "").strip().split())
    if not text:
        return ""
    lowered = text.lower()
    pattern_map = [
        (r"\blooking forward to\b", "looking forward to ~"),
        (r"\bas long as\b", "as long as ..."),
        (r"\bremind me of\b", "remind me of ~"),
        (r"\btake it one step at a time\b", "take it one step at a time"),
        (r"\bstumble upon\b", "stumble upon ~"),
        (r"\bstay hydrated\b", "stay hydrated"),
        (r"\bslip .+? into .+?\b", "slip ~ into ..."),
        (r"\bscope out\b", "scope out ~"),
        (r"\bwork .+? into\b", "work ~ into ..."),
    ]
    for pattern, label in pattern_map:
        if re.search(pattern, lowered):
            return label
    deserve_match = re.search(r"\bdeserve\b\s+(.+)", text, flags=re.IGNORECASE)
    if deserve_match:
        tail = deserve_match.group(1).strip(" .!?")
        if tail:
            return f"deserve {tail}"
    if re.match(r"^(I|I'm|I’d|I'd|I will|I'll)\b", text):
        trimmed = re.sub(r"^(I|I'm|I’d|I'd|I will|I'll)\s+", "", text, count=1)
        if 2 <= len(trimmed.split()) <= 6:
            return trimmed.strip(" .!?")
    return text.strip(" .!?")


def _growth_skill_label(display_phrase: str, meanings: list[str]) -> str:
    if meanings:
        return meanings[0]
    normalized = normalize_phrase(display_phrase)
    if "~" in display_phrase or "..." in display_phrase:
        return "フレーズを文の型として安定して使えるようになってきた"
    if len(normalized.split()) >= 2:
        return "複数語表現を会話の中で自然につなげられている"
    return "表現の使い分けが安定してきた"


def _higher_priority(current: str, candidate: str) -> str:
    priority_rank = {"High": 3, "Medium": 2, "Low": 1}
    return candidate if priority_rank.get(candidate, 0) > priority_rank.get(current, 0) else current


def _more_natural_pattern_label(expression) -> str:
    note = (expression.note or "").strip()
    if note:
        return note
    if expression.your_phrase and expression.more_natural:
        return f"{expression.your_phrase} -> {expression.more_natural}"
    return expression.more_natural or expression.your_phrase


def _format_growth_lines(items: list[dict], period_label: str) -> str:
    if not items:
        return f"- {period_label}の成長ポイントはまだ十分に記録されていません。"
    lines: list[str] = []
    for item in items:
        if item["type"] == "good_point":
            repeated = " 繰り返し出ている強みです。" if item["count"] >= 2 else ""
            lines.append(f"- {item['label']} (Good points {item['count']}回){repeated}")
            continue
        lines.append(
            f"- 「{item['label']}」が定着寄りです。{item['skill_label']} (status: {item['review_status']}, source: {item.get('source') or 'n/a'}, {item['count']}回登場)"
        )
    return "\n".join(lines[:5])


def _format_weakness_lines(items: list[dict]) -> str:
    if not items:
        return "- この期間の弱点記録はまだありません。"
    lines: list[str] = []
    for item in items:
        if item["type"] == "weak_point":
            lines.append(f"- {item['label']} (Weak points {item['count']}回)")
            continue
        if item["type"] == "more_natural":
            example = item["examples"][0] if item.get("examples") else None
            suffix = ""
            if example:
                suffix = f": {example['your_phrase']} -> {example['more_natural']}"
            lines.append(f"- 修正傾向: {item['label']} ({item['count']}回){suffix}")
            continue
        meaning = f" - {item['meanings'][0]}" if item.get("meanings") else ""
        lines.append(
            f"- 繰り返し迷っている表現: {item['label']}{meaning} ({item['count']}回, status: {item['review_status']})"
        )
    return "\n".join(lines[:6])


def _format_next_expression_lines(items: list[dict], include_status: bool = True) -> str:
    if not items:
        return "- 候補なし"
    lines: list[str] = []
    for item in items:
        meaning = f" - {item['meanings'][0]}" if item.get("meanings") else ""
        status = f", status: {item['review_status']}" if include_status else ""
        lines.append(
            f"- {item.get('display_phrase') or item['phrase']}{meaning} (source: {item.get('source') or 'n/a'}, priority: {item.get('highest_priority') or 'n/a'}{status})"
        )
    return "\n".join(lines[:10])


def _format_learning_theme_lines(items: list[str]) -> str:
    if not items:
        return "- 次回会話で新しい phrase を1つ使う。"
    return "\n".join(f"- {item}" for item in items[:3])


def _format_drill_lines(items: list[dict]) -> str:
    if not items:
        return "- 問題1: 今日の会話で使いたい表現を1つ選ぶ。\n- 回答1: その表現を使った短文を1つ作る。\n- Note1: 強い表現より未定着の表現を優先する。"
    lines: list[str] = []
    for index, item in enumerate(items[:5], start=1):
        lines.append(f"- 問題{index}: {item['question']}")
        lines.append(f"- 回答{index}: {item['answer']}")
        if item.get("note"):
            lines.append(f"- Note{index}: {item['note']}")
    return "\n".join(lines)


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
        lines.append(f"- {item.get('display_phrase') or item.get('phrase', '')}{meaning}{suffix}")
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
