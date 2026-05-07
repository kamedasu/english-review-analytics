from __future__ import annotations

from dataclasses import dataclass

import requests


NOTION_VERSION = "2022-06-28"


@dataclass(frozen=True)
class NotionPage:
    page_id: str
    title: str
    last_edited_time: str | None
    markdown: str


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
        return NotionPage(
            page_id=page_id,
            title=_extract_title(page),
            last_edited_time=page.get("last_edited_time"),
            markdown=blocks_to_markdown(blocks),
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
    lines: list[str] = []
    for block in blocks:
        lines.extend(_block_to_lines(block))
    return "\n".join(lines).strip()


def _block_to_lines(block: dict) -> list[str]:
    block_type = block.get("type")
    data = block.get(block_type, {})
    text = _rich_text_to_plain(data.get("rich_text", []))
    lines: list[str] = []

    if block_type == "heading_1":
        lines.append(f"# {text}")
    elif block_type == "heading_2":
        lines.append(f"## {text}")
    elif block_type == "heading_3":
        lines.append(f"### {text}")
    elif block_type == "bulleted_list_item":
        lines.append(f"- {text}")
        lines.extend(_children_to_indented_lines(block, indent="  "))
    elif block_type == "numbered_list_item":
        lines.append(f"- {text}")
        lines.extend(_children_to_indented_lines(block, indent="  "))
    elif block_type == "paragraph":
        if text:
            lines.append(text)
    elif block_type == "to_do":
        checked = "x" if data.get("checked") else " "
        lines.append(f"- [{checked}] {text}")
    elif block_type == "quote":
        lines.append(f"> {text}")
    elif block_type == "code":
        language = data.get("language", "")
        lines.extend([f"```{language}", text, "```"])

    return lines


def _children_to_indented_lines(block: dict, indent: str) -> list[str]:
    lines: list[str] = []
    for child in block.get("children", []):
        for line in _block_to_lines(child):
            lines.append(f"{indent}{line}")
    return lines


def _rich_text_to_plain(items: list[dict]) -> str:
    return "".join(item.get("plain_text", "") for item in items)


def _extract_title(page: dict) -> str:
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            return _rich_text_to_plain(prop.get("title", []))
    return page.get("id", "")
