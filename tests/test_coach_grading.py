from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import requests
from pydantic import ValidationError
from src.coach.grading import Assessment, OpenAIGrader, grade_answer, grading_input
from src.coach.service import CoachError, Usage, generate_plan
from src.storage.local_storage import LocalStorage
from tests.test_learning_coach import fixture, Provider, _coach_test_app


def assessment():
    return dict(grammar_spelling=70, naturalness=80, expression=80, communication=90,
                strengths_ja='意味が明確に伝わります。', improvements_ja='前置詞を確認しましょう。',
                corrected_en='I felt refreshed after yoga.', enhanced_en='Yoga helped me unwind after a busy week.')


def provider():
    p = Mock(model='test-grader')
    p.grade.return_value = (Assessment(**assessment()), Usage(input_tokens=10, output_tokens=20, total_tokens=30))
    return p


class GradingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.storage = LocalStorage(Path(self.tmp.name))
        self.plan = generate_plan(fixture(), self.storage, provider=Provider())
        self.lesson = self.plan.lessons[0]

    def test_scores_strict_bounds_and_average(self):
        self.assertEqual(Assessment(**assessment()).overall, 80)
        for invalid in [-1, 101, True, '80', 80.5]:
            bad = assessment() | {'grammar_spelling': invalid}
            with self.assertRaises(ValidationError): Assessment(**bad)
        with self.assertRaises(ValidationError): Assessment(**(assessment() | {'overall': 99}))

    def test_same_answer_reused_changed_answer_and_question_isolated(self):
        cache = {}; p = provider()
        first = grade_answer(self.plan, self.lesson, 'First answer', cache, provider=p)
        self.assertEqual(grade_answer(self.plan, self.lesson, 'First answer', cache, provider=p), first)
        grade_answer(self.plan, self.lesson, 'Changed answer', cache, provider=p)
        grade_answer(self.plan, self.plan.lessons[1], 'First answer', cache, provider=p)
        newer = self.plan.model_copy(update={'plan_id': 'new-plan'})
        grade_answer(newer, self.lesson, 'First answer', cache, provider=p)
        self.assertEqual(p.grade.call_count, 4)
        self.assertEqual(first.answer, 'First answer')

    def test_failure_preserves_cache_and_saved_data(self):
        cache = {}; p = provider()
        grade_answer(self.plan, self.lesson, 'Valid answer', cache, provider=p)
        before = deepcopy(cache); saved = self.storage.load_text('coach/latest.json')
        p.grade.side_effect = CoachError('採点に失敗しました。')
        with self.assertRaises(CoachError): grade_answer(self.plan, self.lesson, 'Changed', cache, provider=p)
        self.assertEqual(cache, before)
        self.assertEqual(self.storage.load_text('coach/latest.json'), saved)

    def test_empty_and_long_answer_no_api(self):
        p = provider()
        for answer in [' ', '', 'a'*4001]:
            with self.assertRaises(CoachError): grade_answer(self.plan, self.lesson, answer, {}, provider=p)
        p.grade.assert_not_called()

    def test_minimal_input_legacy_level_and_existing_model_setting(self):
        inputs = grading_input(self.plan, self.lesson, 'Different but valid answer')
        self.assertEqual(set(inputs), {'expressions','situation_ja','example_en','answer_en','learner_level','cefr','practice_level'})
        self.assertEqual(inputs['practice_level'], 'applied')
        legacy = self.plan.model_copy(update={'practice_level':'legacy'})
        self.assertEqual(grading_input(legacy, self.lesson, 'Answer')['practice_level'], 'applied')
        settings = Mock(openai_api_key='secret', openai_model='configured-model')
        with patch('src.coach.grading.get_settings', return_value=settings), patch('src.coach.grading.OpenAIGrader') as factory:
            factory.return_value = provider()
            grade_answer(self.plan, self.lesson, 'An answer', {})
            factory.assert_called_once_with('secret', 'configured-model')

    def test_structured_api_and_fair_prompt(self):
        response = Mock(status_code=200)
        response.json.return_value = {'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(assessment())}]}],
                                     'usage':{'input_tokens':10,'output_tokens':20,'total_tokens':30,
                                              'input_tokens_details':{'cached_tokens':0},
                                              'output_tokens_details':{'reasoning_tokens':5}}}
        inputs = grading_input(self.plan,self.lesson,'An alternative answer')
        with patch('src.coach.grading.requests.post',return_value=response) as post:
            result, usage = OpenAIGrader('secret','configured-model').grade(inputs)
        self.assertEqual(usage.total_tokens,30)
        sent=post.call_args.kwargs['json']
        self.assertEqual(sent['model'],'configured-model')
        self.assertFalse(sent['store'])
        self.assertTrue(sent['text']['format']['strict'])
        self.assertNotIn('overall',sent['text']['format']['schema']['properties'])
        self.assertEqual(json.loads(sent['input']),inputs)
        for phrase in ['NOT the only correct answer','US/UK','contractions','minimal edits','unchanged']:
            self.assertIn(phrase,sent['instructions'])
        self.assertEqual(result.overall,80)
        response.close.assert_called_once()

    def test_invalid_responses_and_transport_errors_are_safe(self):
        for body in ['not json', json.dumps(assessment() | {'communication':101}), '{}']:
            response=Mock(status_code=200)
            response.json.return_value={'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':body}]}]}
            with patch('src.coach.grading.requests.post',return_value=response), self.assertRaises(CoachError) as error:
                OpenAIGrader('secret','model').grade({})
            self.assertNotIn('secret',str(error.exception))
            response.close.assert_called_once()
        for error in [requests.Timeout('secret'),requests.ConnectionError('private answer')]:
            with patch('src.coach.grading.requests.post',side_effect=error), self.assertRaises(CoachError) as caught:
                OpenAIGrader('secret','model').grade({})
            self.assertNotIn('secret',str(caught.exception));self.assertNotIn('private answer',str(caught.exception))
        for body in [{'status':'incomplete'}, {'status':'completed','output':[{'type':'message','content':[{'type':'refusal','refusal':'no'}]}]}]:
            response=Mock(status_code=200);response.json.return_value=body
            with patch('src.coach.grading.requests.post',return_value=response), self.assertRaises(CoachError):
                OpenAIGrader('secret','model').grade({})


class GradingUiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.storage=LocalStorage(Path(self.tmp.name))
        self.plan=generate_plan(fixture(),self.storage,provider=Provider())

    def start(self):
        from streamlit.testing.v1 import AppTest
        return AppTest.from_function(_coach_test_app,args=(self.tmp.name,[r.model_dump(mode='json') for r in fixture()])).run()

    def test_all_questions_no_selectors_and_empty_buttons_disabled(self):
        with patch('src.coach.ui.grade_answer') as grade:
            app=self.start()
            self.assertEqual(len(app.exception),0)
            self.assertEqual(len(app.text_area),len(self.plan.lessons))
            self.assertEqual(len(app.checkbox),len(self.plan.lessons))
            self.assertEqual([x.label for x in app.selectbox],['練習レベル'])
            self.assertTrue(all(b.disabled for b in app.button if b.label=='AIで採点する'))
            app.text_area[0].input('A valid answer').run()
            app.checkbox[1].check().run()
            app.selectbox[0].select_index(0).run()
            grade.assert_not_called()

    def test_grading_only_click_reuses_and_stale_results_hidden(self):
        p=provider()
        real=grade_answer
        def grade(plan,lesson,answer,cache):return real(plan,lesson,answer,cache,provider=p)
        with patch('src.coach.ui.grade_answer',side_effect=grade):
            app=self.start();app.text_area[0].input('First answer').run()
            app.button[1].click().run()
            self.assertEqual(p.grade.call_count,1)
            self.assertTrue(any('総合：80点' in x.value for x in app.markdown))
            app.button[1].click().run();self.assertEqual(p.grade.call_count,1)
            app.text_area[0].input('Changed answer').run()
            self.assertFalse(any('総合：' in x.value for x in app.markdown))
            self.assertTrue(any('回答が変更' in x.value for x in app.caption))
            app.text_area[1].input('Another question answer').run()
            app.button[2].click().run();self.assertEqual(p.grade.call_count,2)
            app.checkbox[0].check().run();app.run()
            self.assertEqual(app.text_area[0].value,'Changed answer')
            self.assertEqual(app.text_area[1].value,'Another question answer')
            self.assertTrue(app.checkbox[0].value)
            app.text_area[0].input('First answer').run();app.button[1].click().run()
            self.assertEqual(p.grade.call_count,2)
            self.assertEqual(len(app.exception),0)

    def test_failure_keeps_input_and_regeneration_isolates_state(self):
        with patch('src.coach.ui.grade_answer',side_effect=CoachError('採点できませんでした。')):
            app=self.start();app.text_area[0].input('Keep my answer').run();app.button[1].click().run()
            self.assertEqual(app.text_area[0].value,'Keep my answer');self.assertEqual(len(app.error),1)
        real=generate_plan
        with patch('src.coach.ui.generate_plan',side_effect=lambda reviews,storage,**kw:real(reviews,storage,provider=Provider(),**kw)):
            app.button[0].click().run()
        self.assertEqual(app.text_area[0].value,'')
        self.assertFalse(any('総合：' in x.value for x in app.markdown))
        self.assertEqual(len(app.exception),0)


def _toggle_coach_app(directory, rows):
    import streamlit as st
    from tests.test_learning_coach import _coach_test_app
    if st.checkbox('Coachを表示', value=True, key='show_coach'):
        _coach_test_app(directory, rows)


class GradingLifecycleTests(unittest.TestCase):
    def test_hidden_tab_widget_cleanup_preserves_answer_grade_and_example(self):
        from streamlit.testing.v1 import AppTest
        with TemporaryDirectory() as directory:
            storage=LocalStorage(Path(directory));plan=generate_plan(fixture(),storage,provider=Provider())
            p=provider(); real=grade_answer
            with patch('src.coach.ui.grade_answer',side_effect=lambda plan,lesson,answer,cache:real(plan,lesson,answer,cache,provider=p)):
                app=AppTest.from_function(_toggle_coach_app,args=(directory,[r.model_dump(mode='json') for r in fixture()])).run()
                app.text_area[0].input('Keep this answer').run();app.button[1].click().run()
                app.checkbox[1].check().run()
                app.checkbox[0].uncheck().run()
                self.assertEqual(len(app.text_area),0)
                app.checkbox[0].check().run()
                self.assertEqual(app.text_area[0].value,'Keep this answer')
                self.assertTrue(app.checkbox[1].value)
                self.assertTrue(any('総合：80点' in x.value for x in app.markdown))
                self.assertEqual(p.grade.call_count,1)
                self.assertEqual(len(app.exception),0)
            self.assertEqual(storage.load_json('coach/latest.json'),plan.model_dump(mode='json'))

    def test_ten_questions_are_all_rendered(self):
        from streamlit.testing.v1 import AppTest
        from tests.test_coach_practice import balanced_reviews
        with TemporaryDirectory() as directory:
            reviews=balanced_reviews();storage=LocalStorage(Path(directory))
            generate_plan(reviews,storage,provider=Provider())
            app=AppTest.from_function(_coach_test_app,args=(directory,[r.model_dump(mode='json') for r in reviews])).run()
            self.assertEqual(len(app.exception),0)
            self.assertEqual(len(app.text_area),10)
            self.assertEqual(sum(b.label=='AIで採点する' for b in app.button),10)
            self.assertEqual(len(app.selectbox),1)
