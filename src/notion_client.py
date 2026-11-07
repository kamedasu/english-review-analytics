from __future__ import annotations

from dataclasses import dataclass
import logging
import math
import random
import re
import threading
import time
from typing import Callable

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


logger = logging.getLogger(__name__)


class _RequestLimiter:
    """One in-flight GET and one cooldown across Streamlit sessions in this process."""

    def __init__(self, *, interval: float = 0.5, clock=None, sleep=None):
        self.interval = interval
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.lock = threading.Lock()
        self.next_request = 0.0
        self.blocked_until = 0.0


_SHARED_LIMITER = _RequestLimiter()


class NotionClient:
    def __init__(self, api_key: str, *, limiter=None, session=None,
                 max_retries: int = 5, max_retry_wait: float = 120.0,
                 jitter: Callable[[], float] | None = None,
                 on_retry: Callable[[str], None] | None = None):
        if not api_key:
            raise NotionClientError("NOTION_API_KEY is required.")
        self.api_key = api_key
        if max_retries < 0 or not math.isfinite(max_retry_wait) or max_retry_wait < 0:
            raise ValueError("Invalid Notion retry limits")
        self.base_url = "https://api.notion.com/v1"
        self.limiter = limiter if limiter is not None else _SHARED_LIMITER
        self.session = session if session is not None else requests.Session()
        if session is None:
            # Retry policy lives only in _request, never in urllib3 as well.
            self.session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
        self.max_retries = max_retries
        self.max_retry_wait = max_retry_wait
        self.jitter = jitter or (lambda: random.uniform(0.0, 0.25))
        self.on_retry = on_retry
        self.request_count = 0
        self.retry_wait = 0.0

    def close(self) -> None:
        self.session.close()

    def _fail(self, reason: str, *, status: int | None = None, retry: int = 0):
        logger.error("Notion failed reason=%s status=%s retry=%d requests=%d",
                     reason, status, retry, self.request_count)
        raise NotionClientError(
            f"Notion同期を中断しました: {reason} "
            f"(HTTP={status}, retries={retry}, requests={self.request_count})。保存済みデータを使用します。"
        ) from None

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
        seen_cursors: set[str] = set()
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
            if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                self._fail("invalid_or_repeated_cursor")
            seen_cursors.add(cursor)
        return blocks

    def _request(self, method: str, path: str, params: dict | None = None) -> dict:
        if method != "GET":
            self._fail("unsupported_method")
        for retry in range(self.max_retries + 1):
            # Hold through the response and cooldown registration so another session
            # cannot send a request before it learns of this session's 429.
            with self.limiter.lock:
                now = self.limiter.clock()
                cooldown = max(0.0, self.limiter.blocked_until - now)
                if self.retry_wait + cooldown > self.max_retry_wait:
                    self._fail("rate_limit_wait_budget_exceeded", retry=retry)
                wait = max(0.0, self.limiter.next_request - now, cooldown)
                if wait:
                    logger.info("Notion waiting seconds=%.3f rate_limited=%s retry=%d requests=%d",
                                wait, bool(cooldown), retry, self.request_count)
                    self.limiter.sleep(wait)
                    self.retry_wait += cooldown
                self.limiter.next_request = self.limiter.clock() + self.limiter.interval
                self.request_count += 1
                try:
                    response = self.session.request(
                        method, f"{self.base_url}{path}",
                        headers={"Authorization": f"Bearer {self.api_key}",
                                 "Notion-Version": NOTION_VERSION,
                                 "Content-Type": "application/json"},
                        params=params, timeout=(10, 30), allow_redirects=False,
                    )
                except requests.Timeout:
                    self._fail("timeout", retry=retry)
                except requests.RequestException:
                    self._fail("connection_error", retry=retry)
                try:
                    status = response.status_code
                    logger.info("Notion response status=%d retry=%d requests=%d",
                                status, retry, self.request_count)
                    if status in (429, 529):
                        # Inspect only the reason needed by policy; never echo body text.
                        try:
                            body = response.json()
                        except ValueError:
                            body = {}
                        additional = body.get("additional_data", {}) if isinstance(body, dict) else {}
                        if (isinstance(additional, dict)
                                and additional.get("rate_limit_reason") == "public_api_request_blocked"):
                            self._fail("api_access_blocked", status=status, retry=retry)
                        header = response.headers.get("Retry-After", "").strip()
                        delay = float(header) if re.fullmatch(r"[0-9]+", header) else None
                        if delay is None:
                            delay = min(2 ** retry, 30) + self.jitter()
                        self.limiter.blocked_until = max(
                            self.limiter.blocked_until, self.limiter.clock() + delay)
                        if retry >= self.max_retries:
                            self._fail("rate_limit_retry_exhausted", status=status, retry=retry)
                        if self.retry_wait + delay > self.max_retry_wait:
                            self._fail("rate_limit_wait_budget_exceeded", status=status, retry=retry)
                        logger.warning("Notion retry status=%d retry=%d wait=%.3f requests=%d",
                                       status, retry + 1, delay, self.request_count)
                        if self.on_retry is not None:
                            self.on_retry(f"Notion HTTP {status}: レート制限・過負荷のため{delay:.2f}秒以上待機し再試行します（{retry + 1}/{self.max_retries}）。")
                        continue
                    if not 200 <= status < 300:
                        self._fail("http_error", status=status, retry=retry)
                    try:
                        result = response.json()
                    except ValueError:
                        self._fail("invalid_json", status=status, retry=retry)
                    if not isinstance(result, dict):
                        self._fail("invalid_response", status=status, retry=retry)
                    return result
                finally:
                    response.close()



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
