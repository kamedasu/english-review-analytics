from __future__ import annotations

from collections.abc import MutableMapping

import streamlit as st

from src.coach.candidates import evidence
from src.coach.service import CoachError, generate_plan, is_stale, load_plan
from src.models import Review
from src.storage import StorageError, get_storage

from src.rag.ui_helpers import COACH_TAB_LABEL
SECTIONS = ('Next Conversation Mission', 'Review Practice')


def widget_key(plan_id: str, candidate_id: str, field: str) -> str:
    return f'coach_widget:{plan_id}:{candidate_id}:{field}'


def restore_widget(state: MutableMapping, key: str, default):
    # Streamlit cleans up widgets on hidden tabs. Keep a separate non-widget copy.
    if key not in state:
        state[key] = state.get('coach_answers', {}).get(key, default)


def remember_widget(state: MutableMapping, key: str):
    saved = dict(state.get('coach_answers', {}))
    saved[key] = state[key]
    state['coach_answers'] = saved


def render_learning_coach(reviews: list[Review]) -> None:
    st.subheader(COACH_TAB_LABEL)
    st.caption('学習記録をもとにした提案です。記録にないことだけで、実際に使えていないとは判断しません。')
    try:
        storage = get_storage()
        plan = load_plan(storage)
    except (CoachError, StorageError):
        st.error('保存済みプランを読み込めません。ストレージの設定を確認してください。')
        return
    if plan is not None and is_stale(plan, reviews):
        st.info('新しい学習データがあります。保存済みプランは生成時の記録に基づきます。必要な場合だけRegenerateしてください。')
    st.caption('生成時のみ候補情報をOpenAIへ送信します。通常表示・タブ切り替えでは生成しません。')
    if st.button('Regenerate' if plan else 'Generate Learning Plan', key='coach_generate', disabled=not reviews):
        st.session_state['coach_generation_attempts'] = st.session_state.get('coach_generation_attempts', 0) + 1
        try:
            with st.spinner('学習候補からミッションと復習問題を生成しています…'):
                plan = generate_plan(reviews, storage, regenerate=plan is not None)
        except CoachError as exc:
            st.error(str(exc))
    if not reviews:
        st.info('保存済みレビューがありません。')
    if plan is None:
        st.info('Generate Learning Planで、最大3つのミッションと最大10問の復習問題を作成できます。')
        return
    st.caption(f'生成日時 {plan.generated_at:%Y-%m-%d %H:%M UTC} ｜ モデル {plan.model} ｜ 保存成功 {plan.generation_number}回')
    st.caption(f'元データ {plan.data_version[:16]}… / {plan.review_count}レビュー ｜ 今回 {plan.usage.total_tokens} tokens '
               f'｜ 保存成功分累計 {plan.cumulative_usage.total_tokens} tokens（失敗分は含みません）')
    st.caption(f'このセッションの生成操作 {st.session_state.get("coach_generation_attempts", 0)}回。金額はOpenAIの利用状況で確認してください。')
    section_key = widget_key('navigation', 'section', 'selected')
    restore_widget(st.session_state, section_key, SECTIONS[0])
    section = st.segmented_control('Coach', SECTIONS, key=section_key, on_change=remember_widget,
                                  args=(st.session_state, section_key))
    if section == SECTIONS[0]:
        if len(plan.missions) < 3:
            st.info(f'候補が少ないため、今回は{len(plan.missions)}件のミッションです。')
        st.write(f'目標：{len(plan.missions)}つのうち{min(2, len(plan.missions))}つ以上を、次回の英会話で自然に使ってみましょう。')
        for i, lesson in enumerate(plan.missions, 1):
            c, draft = lesson.candidate, lesson.generated
            with st.expander(f'Mission {i}：{c.expression}', expanded=True):
                st.write('意味：' + draft.meaning_ja)
                st.write('記録から確認できること：' + evidence(c))
                st.write('AIの選定理由・学習提案（解釈）：' + draft.selection_reason_ja)
                st.write('使用場面（想定）：' + draft.situation_ja)
                st.write('例文：' + draft.example_en)
                render_sources(c)
    elif section == SECTIONS[1]:
        st.caption('英文を考えてから答えを表示してください。模範解答は一例で、他にも自然な言い方があります。自己採点で復習できます。')
        for i, lesson in enumerate(plan.lessons, 1):
            c, draft = lesson.candidate, lesson.generated
            # Keep the target English and English-bearing Topic out of the question title.
            with st.expander(f'Question {i}：{draft.meaning_ja}', expanded=i == 1):
                st.write(draft.question_ja)
                st.caption('ヒント：' + draft.situation_ja)
                answer_key = widget_key(plan.plan_id, c.candidate_id, 'answer')
                restore_widget(st.session_state, answer_key, '')
                st.text_area('自分の英文', key=answer_key, on_change=remember_widget, args=(st.session_state, answer_key))
                visible_key = widget_key(plan.plan_id, c.candidate_id, 'visible')
                restore_widget(st.session_state, visible_key, False)
                visible = st.checkbox('答えを見る', key=visible_key, on_change=remember_widget, args=(st.session_state, visible_key))
                if visible:
                    st.write('復習表現：' + c.expression)
                    st.write('模範解答の一例：' + draft.answer_en)
                    st.write('使い方のポイント：' + draft.usage_tip_ja)
                    render_sources(c)
                score_key = widget_key(plan.plan_id, c.candidate_id, 'score')
                restore_widget(st.session_state, score_key, '未採点')
                st.selectbox('自己採点', ['未採点', '自然に言えた', 'ヒントがあれば言えた', 'もう一度練習する'],
                             key=score_key, on_change=remember_widget, args=(st.session_state, score_key))


def render_sources(candidate):
    with st.expander('出典（生成時のレビュー）'):
        for source in candidate.sources:
            st.caption(f'{source.date} ｜ {source.topic} ｜ review_id: {source.review_id}')
