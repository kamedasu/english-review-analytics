from datetime import date
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests

from src.models import Review, PhraseCard, MoreNaturalExpression
from src.coach.candidates import extract_candidates, data_version, normalize
from src.coach.service import (CoachError, OpenAICoach, PLAN_PATH, Usage,
                               generate_plan, load_plan, is_stale)
from src.coach.ui import restore_widget, remember_widget, widget_key, COACH_TAB_LABEL
from src.storage.local_storage import LocalStorage
from src.storage.s3_storage import S3Storage
from src.storage.base import StorageError
from src.rag.ui_helpers import selected_main_tab, MAIN_TAB_STATE_KEY, ASK_HISTORY_TAB_LABEL


def fixture():
    return [
        Review(review_id='old', date=date(2026, 9, 1), topic='Yoga and coffee',
               phrase_cards=[PhraseCard(phrase='unwind', meaning='リラックスする'),
                             PhraseCard(phrase='catch up', meaning='近況を話す'),
                             PhraseCard(phrase='give it a try', meaning='試してみる')],
               more_natural_expressions=[MoreNaturalExpression(more_natural='take a break')],
               weak_points=['"unwind" を復習する']),
        Review(review_id='second', date=date(2026, 9, 2), topic='Friends',
               phrase_cards=[PhraseCard(phrase=' “UNWIND.” ', meaning='気分転換する')],
               weak_points=['unwind'], expressions_to_add=['look forward to']),
        Review(review_id='recent', date=date(2026, 11, 1), topic='Running',
               phrase_cards=[PhraseCard(phrase='keep going')],
               words_and_phrases_actually_used=['I will keep going.']),
    ]


def payload(candidates):
    return {'items': [{'candidate_id': c.candidate_id, 'meaning_ja': '気分転換する',
                       'selection_reason_ja': '日常会話に取り入れる練習をしましょう。',
                       'situation_ja': '仕事の後に友人と話します。', 'example_en': f'I like to {c.expression}.',
                       'question_ja': '仕事の後に気分転換すると伝えてください。',
                       'answer_en': f'I like to {c.expression} after work.',
                       'usage_tip_ja': '自然な場面で使いましょう。'} for c in candidates]}


class Provider:
    model = 'mock-coach-model'
    def __init__(self):
        self.calls = 0
    def generate(self, candidates):
        self.calls += 1
        return payload(candidates), Usage(input_tokens=100, output_tokens=200, total_tokens=300)


class CandidateTests(unittest.TestCase):
    def test_extracts_supported_fields_and_exact_sources(self):
        reviews = fixture()
        candidates = extract_candidates(reviews)
        self.assertEqual({normalize(c.expression) for c in candidates},
                         {'unwind', 'catch up', 'give it a try', 'take a break', 'look forward to', 'keep going'})
        for c in candidates:
            for source in c.sources:
                original = next(r for r in reviews if r.review_id == source.review_id)
                self.assertEqual((source.date, source.topic), (original.date, original.topic))

    def test_normalizes_case_punctuation_whitespace_unicode_without_merging_words(self):
        self.assertEqual(normalize(' “Catch  UP!” '), 'catch up')
        self.assertEqual(normalize('I’m ready'), normalize("I'm ready"))
        c = next(c for c in extract_candidates(fixture()) if normalize(c.expression) == 'unwind')
        self.assertEqual(c.occurrence_count, 2)
        self.assertEqual(c.weak_review_count, 2)
        self.assertEqual(c.days_since_seen, 60)

    def test_repeated_weakness_and_old_phrases_rank_before_recent(self):
        cs = extract_candidates(fixture())
        self.assertEqual(normalize(cs[0].expression), 'unwind')
        self.assertEqual(normalize(cs[-1].expression), 'keep going')
        self.assertEqual(cs[-1].last_recorded_use, date(2026, 11, 1))

    def test_does_not_treat_model_examples_as_actual_use(self):
        reviews = fixture()
        reviews[-1].phrase_cards[0].example = 'I unwind every day.'
        unwind = next(c for c in extract_candidates(reviews) if normalize(c.expression) == 'unwind')
        self.assertIsNone(unwind.last_recorded_use)
        reviews[-1].my_draft = ['I like to unwind.']
        unwind = next(c for c in extract_candidates(reviews) if normalize(c.expression) == 'unwind')
        self.assertEqual(unwind.last_recorded_use, date(2026, 11, 1))

    def test_boundaries_do_not_match_embedded_words(self):
        reviews = [Review(review_id='a', date=date(2026, 1, 1), phrase_cards=[PhraseCard(phrase='run')]),
                   Review(review_id='b', date=date(2026, 2, 1), words_and_phrases_actually_used=['brunch'])]
        self.assertIsNone(extract_candidates(reviews)[0].last_recorded_use)

    def test_does_not_extract_japanese_advice_or_arbitrary_raw_text(self):
        r = Review(review_id='r', date=date(2026, 1, 1), weak_points=['過去形を練習しましょう'],
                   raw_markdown='invented expression', comment='invented expression')
        self.assertEqual(extract_candidates([r]), [])

    def test_limits_and_empty(self):
        self.assertEqual(extract_candidates([]), [])
        r = Review(review_id='r', date=date(2026, 1, 1), phrase_cards=[PhraseCard(phrase=f'phrase {i}') for i in range(20)])
        self.assertEqual(len(extract_candidates([r])), 10)
        self.assertEqual(extract_candidates([r], limit=0), [])

    def test_fingerprint_is_order_independent_and_tracks_content_and_sources(self):
        reviews = fixture()
        first = data_version(reviews)
        self.assertEqual(first, data_version(list(reversed(reviews))))
        reviews[0].topic = 'Changed'
        self.assertNotEqual(first, data_version(reviews))
        with self.assertRaises(ValueError):
            extract_candidates([reviews[0], reviews[0]])


class CoachStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = LocalStorage(Path(self.tmp.name))
        self.provider = Provider()
        self.reviews = fixture()

    def generate(self, **kwargs):
        return generate_plan(self.reviews, self.storage, provider=self.provider, **kwargs)

    def test_generates_three_missions_and_up_to_ten_practice_items_and_reloads(self):
        plan = self.generate()
        self.assertEqual(len(plan.missions), 3)
        self.assertEqual(len(plan.lessons), 6)
        self.assertEqual(load_plan(self.storage), plan)
        self.assertFalse(is_stale(plan, self.reviews))
        self.assertEqual(plan.usage.total_tokens, 300)

    def test_exactly_ten_questions_when_many_candidates(self):
        self.reviews[0].phrase_cards += [PhraseCard(phrase=f'phrase {i}') for i in range(15)]
        self.assertEqual(len(self.generate().lessons), 10)

    def test_insufficient_candidates_reduce_count_and_empty_never_calls_api(self):
        self.reviews = [Review(review_id='single', date=date(2026, 1, 1), phrase_cards=[PhraseCard(phrase='unwind')])]
        self.assertEqual(len(self.generate().missions), 1)
        other = LocalStorage(Path(self.tmp.name)/'empty')
        with self.assertRaises(CoachError):
            generate_plan([], other, provider=self.provider)
        self.assertEqual(self.provider.calls, 1)
        self.assertFalse(other.exists(PLAN_PATH))

    def test_rerenders_and_stale_plans_never_regenerate_implicitly(self):
        original = self.generate()
        for _ in range(3):
            self.assertEqual(self.generate(), original)
        self.reviews[0].weak_points.append('New advice')
        self.assertTrue(is_stale(original, self.reviews))
        self.assertEqual(self.generate(), original)
        self.assertEqual(self.provider.calls, 1)
        updated = self.generate(regenerate=True)
        self.assertEqual(self.provider.calls, 2)
        self.assertNotEqual(original.plan_id, updated.plan_id)
        self.assertEqual(updated.generation_number, 2)
        self.assertEqual(updated.cumulative_usage.total_tokens, 600)

    def test_unknown_duplicate_missing_extra_and_invalid_fields_never_save(self):
        original = self.generate()
        mutations = [lambda x:x['items'][0].update(candidate_id='invented'),
                     lambda x:x['items'].append(x['items'][0]), lambda x:x['items'].pop(),
                     lambda x:x['items'][0].update(source_review_id='fake'),
                     lambda x:x['items'][0].update(answer_en=''),
                     lambda x:x['items'][0].update(meaning_ja=123),
                     lambda x:x['items'][0].update(usage_tip_ja=' '),
                     lambda x:x['items'][0].update(meaning_ja='English only'),
                     lambda x:x['items'][0].update(answer_en='日本語だけです')]
        for mutation in mutations:
            value = payload(extract_candidates(self.reviews));mutation(value)
            bad = Mock(model='mock')
            bad.generate.return_value = (value, Usage())
            with self.subTest(mutation=mutation), self.assertRaises(CoachError):
                generate_plan(self.reviews, self.storage, provider=bad, regenerate=True)
            self.assertEqual(load_plan(self.storage), original)

    def test_provider_failure_keeps_saved_plan(self):
        original = self.generate()
        bad = Mock()
        bad.generate.side_effect = CoachError('API failed')
        with self.assertRaises(CoachError):
            generate_plan(self.reviews, self.storage, provider=bad, regenerate=True)
        self.assertEqual(load_plan(self.storage), original)

    def test_save_failure_keeps_plan(self):
        original = self.generate()
        with patch.object(self.storage, 'save_json', side_effect=StorageError('write failed')):
            with self.assertRaises(CoachError):
                self.generate(regenerate=True)
        self.assertEqual(load_plan(self.storage), original)

    def test_original_components_unchanged(self):
        for p in ['raw/a.md', 'processed/reviews.json', 'state/state.json', 'chroma/sentinel']:
            self.storage.save_text(p, 'unchanged')
        before = {p: self.storage.load_text(p) for prefix in ['raw', 'processed', 'state', 'chroma'] for p in self.storage.list_files(prefix)}
        self.generate()
        self.assertEqual(before, {p: self.storage.load_text(p) for p in before})

    def test_sources_and_expressions_are_never_model_controlled(self):
        plan = self.generate()
        for lesson in plan.lessons:
            self.assertTrue(any(normalize(lesson.candidate.expression) == normalize(c.expression)
                                for c in extract_candidates(self.reviews)))
            for source in lesson.candidate.sources:
                self.assertEqual(source, next(s for c in extract_candidates(self.reviews)
                                              if c.candidate_id == lesson.candidate.candidate_id for s in c.sources
                                              if s.review_id == source.review_id))


class CoachS3Tests(CoachStorageTests):
    def setUp(self):
        super().setUp()
        from botocore.exceptions import ClientError
        objects = {}
        client = Mock()
        def head(**kw):
            if kw['Key'] not in objects:
                raise ClientError({'Error': {'Code': '404'}}, 'HeadObject')
            return {}
        client.head_object.side_effect = head
        client.get_object.side_effect = lambda **kw: {'Body': io.BytesIO(objects[kw['Key']])}
        client.put_object.side_effect = lambda **kw: objects.__setitem__(kw['Key'], kw['Body'])
        client.get_paginator.return_value.paginate.side_effect = lambda **kw: [{'Contents': [{'Key': k} for k in objects if k.startswith(kw['Prefix'])]}]
        with patch('src.storage.s3_storage.boto3.client', return_value=client):
            self.storage = S3Storage('test-coach', 'data')


class ProviderTests(unittest.TestCase):
    def test_response_api_structured_payload_is_bounded_and_private_text_excluded(self):
        reviews = fixture();reviews[0].raw_markdown = 'PRIVATE FULL REVIEW'
        cs = extract_candidates(reviews)
        reply = Mock(status_code=200)
        reply.json.return_value = {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(payload(cs))}]}], 'usage': {'input_tokens': 100, 'output_tokens': 200, 'total_tokens': 300}}
        with patch('src.coach.service.requests.post', return_value=reply) as post:
            result, usage = OpenAICoach('secret', 'mock').generate(cs)
        request = post.call_args.kwargs['json']
        self.assertNotIn('PRIVATE FULL REVIEW', json.dumps(request))
        self.assertEqual(request['text']['format']['type'], 'json_schema')
        self.assertTrue(request['text']['format']['strict'])
        self.assertFalse(request['store'])
        self.assertLess(len(request['input']), 20000)
        self.assertEqual(result, payload(cs))
        self.assertEqual(usage.total_tokens, 300)

    def test_api_errors_invalid_json_refusal_and_incomplete_are_sanitized(self):
        incomplete = Mock(status_code=200);incomplete.json.return_value = {'status': 'incomplete'}
        refusal = Mock(status_code=200);refusal.json.return_value = {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'refusal', 'refusal': 'PRIVATE'}]}]}
        invalid = Mock(status_code=200);invalid.json.side_effect = ValueError('secret PRIVATE')
        bads = [requests.Timeout('secret PRIVATE'), Mock(status_code=429), incomplete, refusal, invalid]
        for bad in bads:
            with self.subTest(bad=type(bad).__name__):
                context = patch('src.coach.service.requests.post', side_effect=bad) if isinstance(bad, Exception) else patch('src.coach.service.requests.post', return_value=bad)
                with context, self.assertRaises(CoachError) as error:
                    OpenAICoach('secret', 'mock').generate(extract_candidates(fixture()))
                self.assertNotIn('secret', str(error.exception))
                self.assertNotIn('PRIVATE', str(error.exception))
        with self.assertRaises(CoachError):
            OpenAICoach('', 'mock').generate(extract_candidates(fixture()))


class UiStateTests(unittest.TestCase):
    def test_answer_visibility_input_and_self_score_survive_widget_cleanup(self):
        state = {'rag_input_query': 'Keep me', MAIN_TAB_STATE_KEY: ASK_HISTORY_TAB_LABEL}
        for field, default, value in [('visible', False, True), ('answer', '', 'My answer'), ('score', '未採点', '自然に言えた')]:
            key = widget_key('plan', 'candidate', field)
            restore_widget(state, key, default)
            self.assertEqual(state[key], default)
            state[key] = value;remember_widget(state, key)
            del state[key]  # Streamlit removes widgets on hidden tabs.
            restore_widget(state, key, default)
            self.assertEqual(state[key], value)
            other = widget_key('new-plan', 'candidate', field)
            restore_widget(state, other, default)
            self.assertEqual(state[other], default)
        self.assertEqual(state['rag_input_query'], 'Keep me')
        self.assertEqual(selected_main_tab(state), ASK_HISTORY_TAB_LABEL)
        state[MAIN_TAB_STATE_KEY] = COACH_TAB_LABEL
        self.assertEqual(selected_main_tab(state), COACH_TAB_LABEL)

    def test_render_only_reads_saved_plan_and_never_generates(self):
        from src.coach import ui
        with TemporaryDirectory() as directory:
            storage = LocalStorage(Path(directory))
            with patch.object(ui, 'get_storage', return_value=storage), patch.object(ui, 'st') as st, patch.object(ui, 'generate_plan') as generate:
                st.button.return_value = False
                ui.render_learning_coach(fixture())
                generate.assert_not_called()
                self.assertEqual(storage.list_files('coach'), [])

    def test_no_api_on_saved_plan_display_and_answers_initially_hidden(self):
        from src.coach import ui
        with TemporaryDirectory() as directory:
            storage = LocalStorage(Path(directory))
            plan = generate_plan(fixture(), storage, provider=Provider())
            with patch.object(ui, 'get_storage', return_value=storage), patch.object(ui, 'st') as st, patch.object(ui, 'generate_plan') as generate:
                st.session_state = {}
                st.button.return_value = False
                st.segmented_control.return_value = 'Review Practice'
                st.checkbox.return_value = False
                ui.render_learning_coach(fixture())
                generate.assert_not_called()
                writes = [c.args[0] for c in st.write.call_args_list]
                self.assertFalse(any('模範解答' in text for text in writes))
                self.assertEqual(st.checkbox.call_count, len(plan.lessons))
                self.assertTrue(all(not v for k, v in st.session_state.items() if k.endswith(':visible')))


def _coach_test_app(directory, rows):
    from pathlib import Path
    from unittest.mock import patch
    from src.coach.ui import render_learning_coach
    from src.models import Review
    from src.storage.local_storage import LocalStorage
    with patch('src.coach.ui.get_storage', return_value=LocalStorage(Path(directory))):
        render_learning_coach([Review.model_validate(r) for r in rows])


class StreamlitCoachTests(unittest.TestCase):
    def test_real_widgets_keep_answers_when_switching_sections(self):
        from streamlit.testing.v1 import AppTest
        with TemporaryDirectory() as directory:
            storage = LocalStorage(Path(directory))
            plan = generate_plan(fixture(), storage, provider=Provider())
            app = AppTest.from_function(_coach_test_app, args=(directory, [r.model_dump(mode='json') for r in fixture()]))
            app.run()
            self.assertEqual(len(app.exception), 0)
            app.session_state[widget_key('navigation', 'section', 'selected')] = 'Review Practice'
            app.run()
            self.assertEqual(len(app.exception), 0)
            self.assertFalse(app.checkbox[0].value)
            app.text_area[0].input('My own answer').run()
            app.checkbox[0].check().run()
            self.assertEqual(len(app.exception), 0)
            self.assertTrue(app.checkbox[0].value)
            app.session_state[widget_key('navigation', 'section', 'selected')] = 'Next Conversation Mission'
            app.run()
            self.assertEqual(len(app.text_area), 0)
            app.session_state[widget_key('navigation', 'section', 'selected')] = 'Review Practice'
            app.run()
            self.assertEqual(app.text_area[0].value, 'My own answer')
            self.assertTrue(app.checkbox[0].value)
            self.assertEqual(load_plan(storage), plan)


def _main_tabs_test_app():
    from unittest.mock import patch
    import streamlit as st
    import app
    from src.data_loader import LoadDebugInfo, LoadResult
    from tests.test_learning_coach import fixture
    with patch.object(app, 'cached_load_reviews', return_value=LoadResult(fixture(), LoadDebugInfo())), \
         patch.object(app, 'render_analytics', side_effect=lambda _:st.write('Analytics sentinel')), \
         patch.object(app, 'render_ask_my_english_history', side_effect=lambda:st.write('History sentinel')), \
         patch.object(app, 'render_learning_coach', side_effect=lambda _:st.write('Coach sentinel')):
        app.main()


class MainTabsTests(unittest.TestCase):
    def test_three_main_tabs_preserve_selection_and_ask_state(self):
        from streamlit.testing.v1 import AppTest
        app = AppTest.from_function(_main_tabs_test_app)
        app.run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual([tab.label for tab in app.tabs], ['Analytics', 'Ask My English History', 'AI Learning Coach'])
        app.session_state['rag_input_query'] = 'My retained question'
        for selected, sentinel in [('AI Learning Coach', 'Coach sentinel'), ('Ask My English History', 'History sentinel'), ('Analytics', 'Analytics sentinel')]:
            app.session_state[MAIN_TAB_STATE_KEY] = selected
            app.run()
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(app.session_state['rag_input_query'], 'My retained question')
            self.assertIn(sentinel, [m.value for m in app.markdown])
