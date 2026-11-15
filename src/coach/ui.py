from __future__ import annotations

from collections.abc import MutableMapping

import streamlit as st

from src.coach.grading import grade_answer, MAX_ANSWER_LENGTH
from src.coach.service import CoachError, Draft, generate_plan, is_stale, load_plan
from src.coach.profile import LEARNER_PROFILE, DEFAULT_PRACTICE_LEVEL, PRACTICE_LEVELS, CATEGORY_LABELS
from src.models import Review
from src.storage import StorageError, get_storage
from src.rag.ui_helpers import COACH_TAB_LABEL


def widget_key(plan_id: str, candidate_id: str, field: str) -> str:
    return f'coach_widget:{plan_id}:{candidate_id}:{field}'


def restore_widget(state: MutableMapping, key: str, default):
    # Keep a non-widget copy when Streamlit cleans up hidden questions/tabs.
    if key not in state:
        state[key] = state.get('coach_answers', {}).get(key, default)


def remember_widget(state: MutableMapping, key: str):
    saved = dict(state.get('coach_answers', {}))
    saved[key] = state[key]
    state['coach_answers'] = saved


def render_learning_coach(reviews: list[Review]) -> None:
    st.subheader('Review Practice')
    level_key = widget_key('navigation', 'practice_level', 'selected')
    restore_widget(st.session_state, level_key, PRACTICE_LEVELS[DEFAULT_PRACTICE_LEVEL]['label'])
    labels = {setting['label']: key for key, setting in PRACTICE_LEVELS.items()}
    if st.session_state[level_key] not in labels:
        st.session_state[level_key] = PRACTICE_LEVELS[DEFAULT_PRACTICE_LEVEL]['label']
    selected_level = st.selectbox('練習レベル', list(labels), key=level_key,
                                  on_change=remember_widget, args=(st.session_state, level_key))
    level = labels.get(selected_level, DEFAULT_PRACTICE_LEVEL)
    try:
        storage = get_storage()
        plan = load_plan(storage)
    except (CoachError, StorageError):
        st.error('保存済みの練習を読み込めません。ストレージの設定を確認してください。')
        return
    if st.button('Regenerate' if plan else 'Generate Learning Plan', key='coach_generate', disabled=not reviews):
        st.session_state['coach_generation_attempts'] = st.session_state.get('coach_generation_attempts', 0) + 1
        try:
            with st.spinner('練習を作成しています…'):
                plan = generate_plan(reviews, storage, regenerate=plan is not None, practice_level=level)
        except CoachError as exc:
            st.error(str(exc))
    if plan is None:
        st.info('保存済みレビューから、最大10問の練習を作成できます。' if reviews else '保存済みレビューがありません。')
        return
    if is_stale(plan, reviews):
        st.info('保存済みの練習を表示しています。Regenerateで新しい練習に更新できます。')
    shown_level = PRACTICE_LEVELS.get(plan.practice_level, {}).get('label', '以前のプラン')
    if plan.practice_level != level:
        st.info(f'選択中：{PRACTICE_LEVELS[level]["label"]}／表示中：{shown_level}。Regenerateを押すと選択したレベルで作成します。')
    st.caption(f'CEFR {LEARNER_PROFILE.cefr} ｜ {shown_level} ｜ {len(plan.lessons)}問')
    st.caption('例文は一例です。他にも自然な答えがあります。')
    st.caption('AI採点は学習支援の目安です。厳密な英語能力試験ではありません。')
    for index, lesson in enumerate(plan.lessons):
        category = '実践' if lesson.category == 'conversation' else CATEGORY_LABELS[lesson.category]
        with st.container(border=True):
            render_question(plan, lesson, index, category)


def render_question(plan, lesson, index: int, category: str) -> None:
    candidate, draft = lesson.candidate, lesson.generated
    st.markdown(f'### Question {index+1}：{category}')
    targets = [candidate.expression, *(target.expression for target in candidate.related_targets)]
    st.write('練習表現：' + ' / '.join(targets))
    st.write('シチュエーション：' + (draft.question_ja if isinstance(draft, Draft) else draft.situation_ja))
    answer_key = widget_key(plan.plan_id, candidate.candidate_id, 'answer')
    restore_widget(st.session_state, answer_key, '')
    answer = st.text_area('自分の英文', key=answer_key, max_chars=MAX_ANSWER_LENGTH,
                          on_change=remember_widget, args=(st.session_state, answer_key))
    result_key = widget_key(plan.plan_id, candidate.candidate_id, 'assessment')
    if st.button('AIで採点する', key=widget_key(plan.plan_id, candidate.candidate_id, 'grade'),
                 disabled=not answer.strip()):
        cache = st.session_state.setdefault('coach_grade_cache', {})
        try:
            with st.spinner('英文を確認しています…'):
                record = grade_answer(plan, lesson, answer, cache)
            st.session_state[result_key] = record
        except CoachError as exc:
            st.error(str(exc))
    record = st.session_state.get(result_key)
    if record is not None:
        if record.answer != answer:
            st.caption('回答が変更されています。採点ボタンを押すと現在の回答を確認できます。')
        else:
            result = record.assessment
            st.write(f'総合：{result.overall:g}点 / 100')
            st.write(f'文法・スペル：{result.grammar_spelling} ／ 自然さ：{result.naturalness} ／ '
                     f'表現力：{result.expression} ／ 意図の伝達：{result.communication}')
            st.write('良かった点：' + result.strengths_ja)
            st.write('改善ポイント：' + result.improvements_ja)
            st.write('添削した英文：' + result.corrected_en)
            st.write('ワンランク上の英文：' + result.enhanced_en)
    visible_key = widget_key(plan.plan_id, candidate.candidate_id, 'visible')
    restore_widget(st.session_state, visible_key, False)
    if st.checkbox('例文を見る', key=visible_key, on_change=remember_widget, args=(st.session_state, visible_key)):
        st.write('例文：' + (draft.answer_en if isinstance(draft, Draft) else draft.example_en))
