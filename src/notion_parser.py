from __future__ import annotations

import re
from datetime import date

from src.models import MoreNaturalExpression, PhraseCard, Review
from src.utils.dates import parse_date
from src.utils.hashing import content_hash


REVIEW_HEADER_RE = re.compile(
    r"(?im)^#\s+(\d{4}[-/]\d{1,2}[-/]\d{1,2})\s+(line\s+english\s+review|review)\b.*$"
)
CARD_HEADER_RE = re.compile(r"(?m)^###\s+Card\s+\d+.*$")
FieldValue = str | list[str]


def split_reviews(markdown: str) -> list[str]:
    matches = list(REVIEW_HEADER_RE.finditer(markdown))
    if not matches:
        return [markdown.strip()] if markdown.strip() else []

    chunks: list[str] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        chunk = markdown[start:end].strip()
        if chunk:
            chunks.append(chunk)
    return chunks


def parse_reviews_from_markdown(
    markdown: str,
    source_page_id: str = "",
    source_page_title: str = "",
    warnings: list[str] | None = None,
) -> list[Review]:
    reviews: list[Review] = []
    for chunk in split_reviews(markdown):
        review = parse_review(chunk, source_page_id, source_page_title, warnings)
        if review:
            reviews.append(review)
    return reviews


def parse_review(
    markdown: str,
    source_page_id: str = "",
    source_page_title: str = "",
    warnings: list[str] | None = None,
) -> Review | None:
    review_date = _extract_review_date(markdown)
    if review_date is None:
        _add_warning(warnings, f"{source_page_title or source_page_id}: review date not found.")
        return None

    body_before_cards = re.split(r"(?im)^##\s*Phrase\s+Cards\s*$", markdown, maxsplit=1)[0]
    fields = _parse_review_fields(body_before_cards)
    review_hash = content_hash(markdown)
    review_id = f"{source_page_id or 'local'}:{review_date.isoformat()}:{review_hash[:12]}"
    review_type = _extract_review_type(markdown)

    phrase_cards = _parse_phrase_cards(markdown, review_id, review_date)
    words_and_phrases_actually_used = _parse_words_and_phrases_actually_used(markdown)
    more_natural_expressions = _parse_more_natural_expressions_from_markdown(markdown, review_id, review_date)
    if not more_natural_expressions:
        more_natural_expressions = _parse_more_natural_expressions(
            _get_first_field(fields, ["More natural expressions", "More natural expression"]),
            review_id,
            review_date,
        )

    return Review(
        review_id=review_id,
        source_page_id=source_page_id,
        source_page_title=source_page_title,
        review_type=review_type,
        date=review_date,
        duration_minutes=_safe_int(_get_field(fields, "Duration")),
        topic=_normalize_text_field(_get_field(fields, "Topic")),
        situation=_normalize_optional_text(
            _get_field_or_section(fields, markdown, ["Situation"])
        ),
        my_draft=_get_list_field_or_section(fields, markdown, ["My draft", "Draft"]),
        more_natural_version=_get_list_field_or_section(
            fields,
            markdown,
            ["More natural version", "Natural version", "Corrected version"],
        ),
        why_it_was_corrected=_get_list_field_or_section(
            fields,
            markdown,
            ["Why it was corrected", "Why corrected", "Reason"],
        ),
        good_points=_get_list_field_or_section(fields, markdown, ["Good points", "Good point"]),
        expressions_to_add=_get_list_field_or_section(fields, markdown, ["Expressions to add"]),
        expressions_to_use_next_time=_get_list_field_or_section(
            fields,
            markdown,
            ["Expressions to use next time"],
        ),
        weak_points=_get_list_field_or_section(fields, markdown, ["Weak points", "Weak point"]),
        more_natural_expressions=more_natural_expressions,
        words_and_phrases_actually_used=words_and_phrases_actually_used,
        comment=_normalize_comment(_get_field(fields, "Comment"), review_id, warnings),
        phrase_cards=phrase_cards,
        raw_markdown=markdown,
        content_hash=review_hash,
    )


def _extract_review_date(markdown: str) -> date | None:
    date_line = re.search(r"(?m)^-\s*Date:\s*(.+?)\s*$", markdown)
    if date_line:
        parsed = parse_date(date_line.group(1))
        if parsed:
            return parsed

    header = REVIEW_HEADER_RE.search(markdown)
    if header:
        return parse_date(header.group(1).replace("/", "-"))
    return None


def _extract_review_type(markdown: str) -> str:
    header = REVIEW_HEADER_RE.search(markdown)
    if not header:
        return "normal"
    return "line" if "line" in header.group(2).lower() else "normal"


def _parse_review_fields(markdown: str) -> dict[str, FieldValue]:
    fields: dict[str, FieldValue] = {}
    lines = markdown.splitlines()
    index = 0

    while index < len(lines):
        line = lines[index]
        match = re.match(r"^-\s*([^:]+):\s*(.*)$", line)
        if not match:
            index += 1
            continue

        key = match.group(1).strip()
        value = match.group(2).strip()

        child_items: list[str] = []
        next_index = index + 1
        while next_index < len(lines):
            child_match = re.match(r"^\s{2,}-\s+(.+)$", lines[next_index])
            if not child_match:
                break
            child_items.append(child_match.group(1).strip())
            next_index += 1

        fields[key] = child_items if child_items else value
        index = next_index

    return fields


def _get_field(fields: dict[str, FieldValue], name: str) -> FieldValue:
    if name in fields:
        return fields[name]
    target = name.lower().strip()
    for key, value in fields.items():
        if key.lower().strip() == target:
            return value
    return []


def _get_first_field(fields: dict[str, FieldValue], names: list[str]) -> FieldValue:
    for name in names:
        value = _get_field(fields, name)
        if value:
            return value
    return []


def _get_field_or_section(fields: dict[str, FieldValue], markdown: str, names: list[str]) -> FieldValue:
    value = _get_first_field(fields, names)
    if value:
        return value
    for name in names:
        section_items = _parse_section_items(markdown, name)
        if section_items:
            return section_items
        section_text = _parse_section_text(markdown, name)
        if section_text:
            return section_text
    return []


def _get_list_field_or_section(fields: dict[str, FieldValue], markdown: str, names: list[str]) -> list[str]:
    return _normalize_list_field(_get_field_or_section(fields, markdown, names))


def _parse_phrase_cards(markdown: str, review_id: str, review_date: date) -> list[PhraseCard]:
    section_match = re.search(r"(?im)^##\s*Phrase\s+Cards\s*$", markdown)
    if not section_match:
        return []

    cards_text = markdown[section_match.end() :]
    matches = list(CARD_HEADER_RE.finditer(cards_text))
    cards: list[PhraseCard] = []

    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(cards_text)
        fields = _parse_card_fields(cards_text[start:end])
        phrase = fields.get("Phrase", "")
        if not phrase:
            continue
        cards.append(
            PhraseCard(
                phrase=phrase,
                meaning=fields.get("Meaning", ""),
                example=fields.get("Example", ""),
                next_review_date=parse_date(fields.get("Next review date", "")),
                priority=fields.get("Priority", ""),
                source_review_id=review_id,
                source_review_date=review_date,
            )
        )
    return cards


def _parse_card_fields(markdown: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in markdown.splitlines():
        match = re.match(r"^-\s*([^:]+):\s*(.*)$", line.strip())
        if match:
            fields[match.group(1).strip()] = match.group(2).strip()
    return fields


def _parse_words_and_phrases_actually_used(markdown: str) -> list[str]:
    section_match = re.search(
        r"(?mis)^##\s*Words\s+and\s+phrases\s+actually\s+used\s*$"
        r"(?P<body>.*?)(?=^##\s+|\Z)",
        markdown,
    )
    if not section_match:
        return []

    items: list[str] = []
    for line in section_match.group("body").splitlines():
        match = re.match(r"^\s*[-*]\s+(.+?)\s*$", line)
        if not match:
            continue
        value = match.group(1).strip()
        if value:
            items.append(value)
    return items


def _parse_section_items(markdown: str, section_name: str) -> list[str]:
    body = _section_body(markdown, section_name)
    if not body:
        return []

    items: list[str] = []
    for line in body.splitlines():
        match = re.match(r"^\s*[-*]\s+(.+?)\s*$", line)
        if not match:
            continue
        value = match.group(1).strip()
        if value:
            items.append(value)
    return items


def _parse_section_text(markdown: str, section_name: str) -> str:
    body = _section_body(markdown, section_name)
    if not body:
        return ""
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    return "\n".join(lines).strip()


def _section_body(markdown: str, section_name: str) -> str:
    pattern = (
        rf"(?mis)^##\s*{re.escape(section_name).replace(r'\\ ', r'\\s+')}\s*$"
        r"(?P<body>.*?)(?=^##\s+|^#\s+|\Z)"
    )
    section_match = re.search(pattern, markdown)
    return section_match.group("body").strip() if section_match else ""


def _parse_more_natural_expressions(
    value: FieldValue,
    review_id: str,
    review_date: date,
) -> list[MoreNaturalExpression]:
    items = _normalize_list_field(value)
    if not items:
        return []

    records: list[MoreNaturalExpression] = []
    current: dict[str, str] = {}

    for item in items:
        key, field_value = _split_key_value(item)
        normalized_key = _normalize_more_natural_key(key)
        if normalized_key:
            if normalized_key == "your_phrase" and current:
                records.append(_more_natural_from_fields(current, review_id, review_date))
                current = {}
            current[normalized_key] = field_value
            continue

        if current:
            current["note"] = " ".join(part for part in [current.get("note", ""), item] if part).strip()
        else:
            current["your_phrase"] = item

    if current:
        records.append(_more_natural_from_fields(current, review_id, review_date))

    return [
        record
        for record in records
        if record.your_phrase or record.more_natural or record.note
    ]


def _parse_more_natural_expressions_from_markdown(
    markdown: str,
    review_id: str,
    review_date: date,
) -> list[MoreNaturalExpression]:
    section_match = re.search(
        r"(?ms)^-\s*More natural expressions:\s*\n(?P<body>.*?)(?=^-\s*[A-Z][^:\n]+:\s*|^##\s+|\Z)",
        markdown,
    )
    if not section_match:
        return []

    body = section_match.group("body").strip()
    if not body:
        return []

    records: list[MoreNaturalExpression] = []
    chunks = re.split(r"(?m)^\s*-\s*Your phrase:\s*", body)
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        fields = _parse_more_natural_chunk(f"Your phrase:\n{chunk}")
        records.append(_more_natural_from_fields(fields, review_id, review_date))

    return [
        record
        for record in records
        if record.your_phrase or record.more_natural or record.note
    ]


def _parse_more_natural_chunk(chunk: str) -> dict[str, str]:
    fields: dict[str, list[str]] = {
        "your_phrase": [],
        "more_natural": [],
        "note": [],
    }
    current_key = ""

    for raw_line in chunk.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        key, value = _split_key_value(line.lstrip("-").strip())
        normalized_key = _normalize_more_natural_key(key)
        if normalized_key:
            current_key = normalized_key
            if value:
                fields[current_key].append(value)
            continue
        if current_key:
            fields[current_key].append(line)

    return {key: " ".join(value).strip() for key, value in fields.items()}


def _more_natural_from_fields(
    fields: dict[str, str],
    review_id: str,
    review_date: date,
) -> MoreNaturalExpression:
    return MoreNaturalExpression(
        your_phrase=fields.get("your_phrase", ""),
        more_natural=fields.get("more_natural", ""),
        note=fields.get("note", ""),
        source_review_id=review_id,
        source_review_date=review_date,
    )


def _split_key_value(value: str) -> tuple[str, str]:
    match = re.match(r"^([^:：]+)[:：]\s*(.*)$", value.strip())
    if not match:
        return "", value.strip()
    return match.group(1).strip(), match.group(2).strip()


def _normalize_more_natural_key(key: str) -> str:
    normalized = key.lower().strip().replace("-", " ").replace("_", " ")
    if normalized in {"your phrase", "original", "before"}:
        return "your_phrase"
    if normalized in {"more natural", "natural", "better", "after"}:
        return "more_natural"
    if normalized in {"note", "notes", "reason", "comment"}:
        return "note"
    return ""


def _normalize_comment(value: FieldValue, review_id: str, warnings: list[str] | None = None) -> str:
    if isinstance(value, list):
        _add_warning(warnings, f"{review_id}: Comment was parsed as list and normalized to text.")
        return "\n".join(item for item in value if item).strip()
    return value.strip()


def _normalize_text_field(value: FieldValue) -> str:
    if isinstance(value, list):
        return " ".join(item for item in value if item).strip()
    return value.strip()


def _normalize_optional_text(value: FieldValue) -> str | None:
    text = _normalize_text_field(value)
    return text or None


def _normalize_list_field(value: FieldValue) -> list[str]:
    if isinstance(value, list):
        return [item.strip() for item in value if item.strip()]
    value = value.strip()
    return [value] if value else []


def _safe_int(value: FieldValue) -> int:
    if isinstance(value, list):
        value = " ".join(value)
    match = re.search(r"\d+", value)
    return int(match.group(0)) if match else 0


def _add_warning(warnings: list[str] | None, message: str) -> None:
    if warnings is not None:
        warnings.append(message)
