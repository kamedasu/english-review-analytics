from __future__ import annotations

from html import escape

import pandas as pd
import streamlit as st

from src.analytics import (
    available_months,
    available_quarters,
    available_years,
    filter_reviews_by_month,
    filter_reviews_by_quarter,
    filter_reviews_by_year,
    more_natural_expressions_to_dataframe,
    summarize_month,
    summarize_quarter,
    summarize_year,
    weak_points_to_dataframe,
)
from src.data_loader import load_local_reviews, load_or_fetch_reviews
from src.llm_summary import generate_period_summary
from src.rag.answerer import RagAnswerer
from src.rag.sync_update import RagSyncUpdateOutcome, run_rag_update_after_sync, sync_completed_successfully
from src.rag.ui_helpers import prepare_rag_answer_request, rag_error_message, source_display


st.set_page_config(page_title="English Review Analytics", layout="wide")

TABLE_STYLE = """
<style>
.learning-table {
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
  margin-top: 0.25rem;
}
.learning-table th,
.learning-table td {
  padding: 0.75rem 0.9rem;
  text-align: left;
  vertical-align: top;
  border: 1px solid rgba(250, 250, 250, 0.08);
  white-space: normal;
  word-break: break-word;
  overflow-wrap: anywhere;
  line-height: 1.55;
}
.learning-table th {
  background: rgba(255, 255, 255, 0.06);
  color: rgba(255, 255, 255, 0.92);
  font-weight: 600;
}
.learning-table tbody tr:nth-child(odd) td {
  background: rgba(255, 255, 255, 0.035);
}
.learning-table tbody tr:nth-child(even) td {
  background: rgba(255, 255, 255, 0.065);
}
</style>
"""


@st.cache_data(show_spinner=False)
def cached_load_reviews(refresh: bool):
    return load_or_fetch_reviews(refresh=refresh)


@st.cache_data(show_spinner=False)
def cached_period_summary(summary, reviews, period_type: str):
    return generate_period_summary(summary, reviews, period_type)


def main() -> None:
    st.title("English Review Analytics")
    st.markdown(TABLE_STYLE, unsafe_allow_html=True)

    with st.sidebar:
        st.header("Data")
        st.caption("通常表示はローカル保存済みデータのみを使います。")
        sync = st.button("Sync from Notion", type="primary")
        if sync:
            cached_load_reviews.clear()
            st.session_state["cache_cleared"] = True

    with st.spinner("Syncing active month from Notion..." if sync else "Loading local reviews..."):
        load_result = load_or_fetch_reviews(refresh=True) if sync else cached_load_reviews(False)

    reviews = load_result.reviews
    debug = load_result.debug
    resolve_cache_event(debug.loaded_at, sync)

    rag_sync_outcome: RagSyncUpdateOutcome | None = None
    if sync and sync_completed_successfully(debug):
        # Re-read the persisted source of truth; do not index transient Notion response data.
        saved_result = load_local_reviews()
        if any(status.status == "エラー" for status in saved_result.debug.page_statuses):
            rag_sync_outcome = RagSyncUpdateOutcome(error_kind="update_failed")
        else:
            with st.spinner("Updating RAG index..."):
                rag_sync_outcome = run_rag_update_after_sync(debug, saved_result.reviews)

    for message in debug.messages:
        st.sidebar.caption(message)
    if debug.sync_requested:
        render_sync_result(debug, rag_sync_outcome)

    if any(item.status == "エラー" for item in debug.page_statuses):
        st.warning("一部または全てのNotionページを取得できませんでした。表示中のデータはローカルキャッシュを含む可能性があります。")

    analytics_tab, history_tab = st.tabs(["Analytics", "Ask My English History"])
    with analytics_tab:
        if not reviews:
            if any(item.status == "エラー" for item in debug.page_statuses):
                st.error("Notionデータを読み込めませんでした。サイドバーのメッセージとNotion API設定を確認してください。")
            else:
                st.warning("ローカル保存済みレビューがありません。必要に応じて Sync from Notion を実行してください。")
        else:
            render_analytics(reviews)

    with history_tab:
        render_ask_my_english_history()


def render_analytics(reviews: list) -> None:
    period_type = st.segmented_control(
        "Aggregation",
        ["Monthly", "Quarterly", "Yearly"],
        default="Monthly",
    )
    selected_period, period_reviews, summary = select_period(reviews, period_type)
    if selected_period is None:
        st.warning("選択できる期間がありません。")
        return

    with st.spinner("Generating summary..."):
        period_summary = cached_period_summary(summary, period_reviews, period_type)
    summary.llm_summary = period_summary.text
    render_metrics(summary)
    render_period_summary(period_type, summary, period_summary)
    render_improvement_focus(period_reviews)


def render_ask_my_english_history() -> None:
    st.subheader("Ask My English History")
    st.caption("過去の英語学習履歴に質問")
    st.caption("例: 過去にも同じミスしてた？ / カフェの話で以前覚えた表現は？ / 最近の弱点は？")

    with st.form("rag_ask_form"):
        query = st.text_area(
            "Question",
            key="rag_input_query",
            placeholder="過去にも同じミスしてた？",
            height=100,
        )
        submitted = st.form_submit_button("Ask")

    query_to_run = prepare_rag_answer_request(submitted, query, st.session_state)
    if query_to_run is not None:
        try:
            with st.spinner("Searching your English history..."):
                st.session_state["rag_answer"] = RagAnswerer().answer(query_to_run)
        except Exception as exc:
            st.session_state["rag_error"] = rag_error_message(exc)

    error = st.session_state.get("rag_error", "")
    if error:
        st.error(error)

    answer = st.session_state.get("rag_answer")
    if answer is None:
        return
    st.markdown("### Answer")
    st.markdown(answer.answer)
    if not answer.sources:
        return

    with st.expander("Sources"):
        for number, source in enumerate(answer.sources, start=1):
            display = source_display(source, number)
            st.markdown(f"#### Source {display.number}")
            if display.date:
                st.write(f"Date: {display.date}")
            if display.type_label:
                st.write(f"Type: {display.type_label}")
            if display.topic:
                st.write(f"Topic: {display.topic}")
            st.write("Text:")
            st.write(display.text)


def select_period(reviews: list, period_type: str):
    if period_type == "Monthly":
        periods = available_months(reviews)
        selected = st.selectbox("Month", periods, index=0)
        return selected, filter_reviews_by_month(reviews, selected), summarize_month(reviews, selected)

    if period_type == "Quarterly":
        periods = available_quarters(reviews)
        selected = st.selectbox("Quarter", periods, index=0)
        return selected, filter_reviews_by_quarter(reviews, selected), summarize_quarter(reviews, selected)

    periods = available_years(reviews)
    selected = st.selectbox("Year", periods, index=0)
    return selected, filter_reviews_by_year(reviews, selected), summarize_year(reviews, selected)


def render_sync_result(debug, rag_sync_outcome: RagSyncUpdateOutcome | None = None) -> None:
    st.info("Sync from Notion completed. 通常表示はローカル保存済みデータを使います。")
    rows = [
        {
            "month": item.synced_month,
            "page_title": item.title,
            "status": item.status,
            "added": item.added_count,
            "updated": item.updated_count,
            "skipped": item.skipped_count,
            "error": item.error,
        }
        for item in debug.page_statuses
    ]
    if rows:
        st.dataframe(rows, width="stretch", hide_index=True)
    if rag_sync_outcome is not None:
        render_rag_sync_update(rag_sync_outcome)


def render_rag_sync_update(outcome: RagSyncUpdateOutcome) -> None:
    if outcome.error_kind == "rebuild_required":
        st.warning("RAG Index requires a full rebuild. Saved review data is safe.")
        return
    if outcome.error_kind:
        st.warning("RAG Index Update failed. Saved review data is safe. Run the incremental index update again later.")
        return
    if outcome.result is None:
        return

    result = outcome.result
    st.markdown("#### RAG Index Update")
    if not (result.added_count or result.changed_count or result.deleted_count):
        st.caption("No changes")
    st.write(
        f"Added: {result.added_count} · Changed: {result.changed_count} · "
        f"Deleted: {result.deleted_count} · Unchanged: {result.unchanged_count} · "
        f"Embedded: {result.embedded_count} · Stored: {result.stored_count}"
    )
    if result.initial_build:
        st.caption("RAG index did not exist, so an initial index was created.")


def render_period_summary(period_type: str, summary, period_summary) -> None:
    st.subheader(f"{period_type} Summary")
    if period_summary.source == "llm":
        st.caption(f"Generated by OpenAI API ({period_summary.model})")
    else:
        st.caption("Generated by rule-based fallback")
    if period_summary.warning:
        st.warning(period_summary.warning)
    st.write(summary.llm_summary)


def render_improvement_focus(reviews: list) -> None:
    st.subheader("Improvement Focus")

    st.markdown("#### Weak Points")
    weak_points_df = weak_points_to_dataframe(reviews)
    if weak_points_df.empty:
        st.caption("この期間には weak points の記録はまだありません。")
    else:
        render_wrapped_table(weak_points_df[["weak_point"]])

    st.markdown("#### More Natural Expressions")
    more_natural_df = more_natural_expressions_to_dataframe(reviews)
    if more_natural_df.empty:
        st.caption("この期間には more natural expressions の記録はまだありません。")
    else:
        render_wrapped_table(more_natural_df)


def render_wrapped_table(df: pd.DataFrame) -> None:
    if df.empty:
        return
    headers = "".join(f"<th>{escape(str(column))}</th>" for column in df.columns)
    rows: list[str] = []
    for _, row in df.fillna("").iterrows():
        cells = "".join(
            f"<td>{escape(str(value)).replace(chr(10), '<br>')}</td>"
            for value in row.tolist()
        )
        rows.append(f"<tr>{cells}</tr>")
    table_html = (
        "<table class='learning-table'>"
        f"<thead><tr>{headers}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )
    st.markdown(table_html, unsafe_allow_html=True)

def resolve_cache_event(loaded_at: str, refresh: bool) -> str:
    signature = str(refresh)
    previous = st.session_state.get("last_load")
    cache_cleared = st.session_state.pop("cache_cleared", False)

    if cache_cleared:
        event = "cache clear + fresh fetch"
    elif previous and previous.get("signature") == signature and previous.get("loaded_at") == loaded_at:
        event = "cache hit"
    else:
        event = "fresh fetch"

    st.session_state["last_load"] = {"signature": signature, "loaded_at": loaded_at}
    return event


def render_metrics(summary) -> None:
    cols = st.columns(6)
    cols[0].metric("Total Study Time", f"{summary.total_duration_minutes} min")
    cols[1].metric("Study Days", summary.study_days)
    cols[2].metric("Longest Streak", f"{summary.longest_streak} days")
    cols[3].metric("Reviews", summary.review_count)
    cols[4].metric("Phrases", summary.phrase_count)
    cols[5].metric("Reused Phrases", summary.reused_phrase_count)


if __name__ == "__main__":
    main()
