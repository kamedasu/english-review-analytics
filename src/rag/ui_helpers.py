from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import dataclass

from src.rag.answerer import RagAnswerGenerationError
from src.rag.chroma_store import RagIndexNotFoundError
from src.rag.embeddings import EmbeddingError
from src.rag.retriever import RetrievedDocument


TYPE_LABELS = {
    "good_point": "Good Point",
    "weak_point": "Weak Point",
    "more_natural_expression": "More Natural Expression",
    "phrase_card": "Phrase Card",
}

MAIN_TAB_STATE_KEY = "main_view"
ANALYTICS_TAB_LABEL = "Analytics"
ASK_HISTORY_TAB_LABEL = "Ask My English History"


@dataclass(frozen=True)
class SourceDisplay:
    number: int
    type_label: str
    date: str
    topic: str
    text: str


def selected_main_tab(session_state: MutableMapping[str, object]) -> str:
    """Return a valid stateful-tab selection without touching RAG input state."""
    selected = session_state.get(MAIN_TAB_STATE_KEY)
    if selected in (ANALYTICS_TAB_LABEL, ASK_HISTORY_TAB_LABEL):
        return str(selected)
    return ANALYTICS_TAB_LABEL


def should_run_rag_answer(submitted: bool, query: str) -> bool:
    return submitted and bool(query.strip())


def prepare_rag_answer_request(
    submitted: bool,
    input_query: str,
    session_state: MutableMapping[str, object],
) -> str | None:
    """Record an explicit Ask submission without reusing the widget state as history."""
    if not submitted:
        return None

    query = input_query.strip()
    if not query:
        session_state["rag_error"] = "質問を入力してください。"
        return None

    session_state["rag_last_query"] = query
    session_state["rag_answer"] = None
    session_state["rag_error"] = ""
    return query


def source_display(source: RetrievedDocument, number: int) -> SourceDisplay:
    metadata = source.document.metadata
    document_type = str(metadata.get("type", ""))
    return SourceDisplay(
        number=number,
        type_label=TYPE_LABELS.get(document_type, document_type),
        date=str(metadata.get("date", "") or ""),
        topic=str(metadata.get("topic", "") or ""),
        text=source.document.text,
    )


def rag_error_message(error: Exception) -> str:
    if isinstance(error, RagIndexNotFoundError):
        return "RAG indexがまだ作成されていません。ターミナルで `python -m src.rag.indexer` を実行してください。"
    if isinstance(error, EmbeddingError):
        return "質問の検索処理でOpenAI APIエラーが発生しました。時間をおいて再度お試しください。"
    if isinstance(error, RagAnswerGenerationError):
        return "回答の生成でOpenAI APIエラーが発生しました。時間をおいて再度お試しください。"
    if isinstance(error, ValueError) and "OPENAI_API_KEY" in str(error):
        return "OPENAI_API_KEYが設定されていません。既存の設定方法を確認してください。"
    return "質問の処理中にエラーが発生しました。設定とネットワーク接続を確認してください。"
