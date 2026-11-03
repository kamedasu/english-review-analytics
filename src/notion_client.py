from __future__ import annotations

from dataclasses import dataclass

from src.notion_parser import REVIEW_HEADER_RE, split_reviews

import requests


NOTION_VERSION = "2022-06-28"


@dataclass(frozen=True)
class NotionReviewBlock:
    source_block_id: str
    markdown: str


@dataclass(frozen=True)
class NotionPage:
    page_id: str
    title: str
    last_edited_time: str | None
    markdown: str
    review_blocks: tuple[NotionReviewBlock, ...] = ()


class NotionClientError(RuntimeError):
    pass


class NotionClient:
    def __init__(self, api_key: str):
        if not api_key:
            raise NotionClientError("NOTION_API_KEY is required.")
        self.api_key = api_key
        self.base_url = "https://api.notion.com/v1"

    def fetch_page_as_markdown(self, page_id: str) -> NotionPage:
        page = self._request("GET", f"/pages/{page_id}")
        blocks = self._fetch_all_blocks(page_id)
        markdown, review_blocks = blocks_to_review_markdown(blocks)
        return NotionPage(
            page_id=page_id,
            title=_extract_title(page),
            last_edited_time=page.get("last_edited_time"),
            markdown=markdown,
            review_blocks=review_blocks,
        )

    def _fetch_all_blocks(self, block_id: str) -> list[dict]:
        blocks: list[dict] = []
        cursor: str | None = None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            response = self._request("GET", f"/blocks/{block_id}/children", params=params)
            for block in response.get("results", []):
                blocks.append(block)
                if block.get("has_children"):
                    children = self._fetch_all_blocks(block["id"])
                    block["children"] = children
            if not response.get("has_more"):
                break
            cursor = response.get("next_cursor")
        return blocks

    def _request(self, method: str, path: str, params: dict | None = None) -> dict:
        try:
            response = requests.request(
                method,
                f"{self.base_url}{path}",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Notion-Version": NOTION_VERSION,
                    "Content-Type": "application/json",
                },
                params=params,
                timeout=30,
            )
        except requests.RequestException as exc:
            raise NotionClientError(f"Notion API request failed: {exc}") from exc
        if response.status_code >= 400:
            raise NotionClientError(f"Notion API error {response.status_code}: {response.text}")
        return response.json()


def blocks_to_markdown(blocks: list[dict]) -> str:
    return blocks_to_review_markdown(blocks)[0]


def blocks_to_review_markdown(blocks: list[dict]) -> tuple[str, tuple[NotionReviewBlock, ...]]:
    lines = [line for block in blocks for line in _render_block(block)]
    markdown = "\n".join(text for text, _ in lines).strip()
    # Match the parser's exact boundaries, but trust IDs only on real heading_1 blocks.
    sources = []
    for text, block_id in lines:
        for line in text.split("\n"):
            if REVIEW_HEADER_RE.match(line):
                sources.append(block_id)
    chunks = split_reviews(markdown)
    if len(sources) != len(chunks) or any(not source for source in sources):
        # An apparent review header inside code/plain text can truncate an earlier
        # real review. Hold the whole page rather than update it with partial text.
        return markdown, tuple(NotionReviewBlock("", chunk) for chunk in chunks)
    return markdown, tuple(NotionReviewBlock(block_id, chunk) for block_id, chunk in zip(sources, chunks))


def _render_block(block: dict, indent: str = "") -> list[tuple[str, str]]:
    block_type = block.get("type")
    data = block.get(block_type, {})
    text = _rich_text_to_plain(data.get("rich_text", []))
    own = []
    if block_type in {"heading_1", "heading_2", "heading_3"}:
        own = [f"{'#' * int(block_type[-1])} {text}"]
    elif block_type in {"bulleted_list_item", "numbered_list_item"}:
        own = [f"- {text}"]
    elif block_type == "paragraph" and text:
        own = [text]
    elif block_type == "to_do":
        own = [f"- [{'x' if data.get('checked') else ' '}] {text}"]
    elif block_type == "quote":
        own = [f"> {text}"]
    elif block_type == "code":
        own = [f"```{data.get('language', '')}", text, "```"]
    block_id = block.get("id")
    source = block_id if block_type == "heading_1" and not indent and isinstance(block_id, str) else ""
    lines = [(indent + line, source) for line in own]
    child_indent = indent + "  " if block_type in {"bulleted_list_item", "numbered_list_item"} else indent
    for child in block.get("children", []):
        lines.extend(_render_block(child, child_indent))
    return lines


def _rich_text_to_plain(items: list[dict]) -> str:
    return "".join(item.get("plain_text", "") for item in items)


def _extract_title(page: dict) -> str:
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            return _rich_text_to_plain(prop.get("title", []))
    return page.get("id", "")
