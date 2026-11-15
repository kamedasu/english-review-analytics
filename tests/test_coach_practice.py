from collections import Counter
from datetime import date
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from src.models import Review, PhraseCard
from src.coach.candidates import extract_candidates, choose_practice_candidates
from src.coach.profile import DEFAULT_PRACTICE_LEVEL, PRACTICE_LEVELS, LOGIC_VERSION
from src.coach.service import (CoachError, OpenAICoach, generate_plan, load_plan, PLAN_PATH,
                               PROMPT_VERSION, is_stale)
from src.storage.local_storage import LocalStorage
from tests.test_learning_coach import Provider, fixture, payload, legacy_draft, _coach_test_app


def balanced_reviews():
    phrases = [f'make room for idea {i}' for i in range(12)]
    return [Review(review_id='balanced', date=date(2026, 11, 1), topic='Yoga coffee hiking',
                   phrase_cards=[PhraseCard(phrase=p) for p in phrases],
                   weak_points=[f'「{p}」の使い分けを混同' for p in phrases[:3]])]


class PracticeGenerationTests(unittest.TestCase):
    def test_balanced_categories_are_disjoint_and_grounded(self):
        reviews = balanced_reviews()
        pool = extract_candidates(reviews, limit=10000)
        selected, categories = choose_practice_candidates(pool)
        self.assertEqual(len(selected), 10)
        self.assertEqual(Counter(categories.values()), {'weakness': 3, 'expression': 4, 'conversation': 3})
        self.assertEqual(len({c.candidate_id for c in selected}), 10)
        for c in selected:
            self.assertIn(c.candidate_id, {x.candidate_id for x in pool})
            if categories[c.candidate_id] == 'weakness':
                self.assertTrue(c.correction_review_count or c.nuance_review_count)

    def test_category_shortage_redistributes_without_repeating(self):
        pool = extract_candidates([Review(review_id='r', date=date(2026, 1, 1),
                    phrase_cards=[PhraseCard(phrase=f'come to terms with issue {i}') for i in range(7)])])
        selected, categories = choose_practice_candidates(pool)
        self.assertEqual(len(selected), 7)
        self.assertNotIn('weakness', categories.values())
        self.assertEqual(len({c.candidate_id for c in selected}), 7)
        self.assertEqual(choose_practice_candidates(pool, limit=0), ([], {}))

    def test_simple_routines_need_evidence_and_never_fill_expansion(self):
        basics = ['I just woke up', 'on my way', 'I usually', 'I often go home',
                  'I usually go home after work every day', 'I just woke up and started my day',
                  'player', 'hang out', 'in front of', 'go back home', 'in / out']
        r = Review(review_id='r', date=date(2026, 1, 1), phrase_cards=[PhraseCard(phrase=p) for p in basics])
        self.assertEqual(extract_candidates([r]), [])
        r.weak_points = ['「on my way」の前置詞の使い分けを混同']
        selected, categories = choose_practice_candidates(extract_candidates([r]))
        self.assertEqual([c.expression for c in selected], ['on my way'])
        self.assertEqual(set(categories.values()), {'weakness'})

    def test_all_levels_save_with_same_intermediate_learner_level(self):
        with TemporaryDirectory() as d:
            storage = LocalStorage(Path(d))
            for level in PRACTICE_LEVELS:
                plan = generate_plan(balanced_reviews(), storage, provider=Provider(), practice_level=level, regenerate=True)
                self.assertEqual(plan.schema_version, 2)
                self.assertEqual(plan.practice_level, level)
                self.assertEqual(plan.learner_level, 'intermediate')
                self.assertEqual(Counter(l.category for l in plan.lessons), {'weakness': 3, 'expression': 4, 'conversation': 3})
                self.assertEqual(load_plan(storage), plan)

    def test_defaults_to_applied_and_level_change_is_not_generation(self):
        with TemporaryDirectory() as d:
            storage = LocalStorage(Path(d));provider = Provider()
            first = generate_plan(fixture(), storage, provider=provider)
            self.assertEqual(first.practice_level, DEFAULT_PRACTICE_LEVEL)
            same = generate_plan(fixture(), storage, provider=provider, practice_level='basic')
            self.assertEqual(first, same)
            self.assertEqual(provider.calls, 1)
            with self.assertRaises(CoachError):
                generate_plan(fixture(), storage, provider=provider, practice_level='unknown')
            self.assertEqual(load_plan(storage), first)

    def test_schema_separates_auxiliary_suggestions_from_learned_expression(self):
        with TemporaryDirectory() as d:
            storage = LocalStorage(Path(d));cs = extract_candidates(fixture())
            value = payload(cs)
            value['items'][0]['support_suggestions'] = ['as it turned out']
            provider = Mock(model='mock');provider.generate.return_value = (value, {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2})
            plan = generate_plan(fixture(), storage, provider=provider)
            self.assertIn('as it turned out', plan.lessons[0].generated.support_suggestions)
            self.assertEqual(plan.lessons[0].support_origin, 'ai_suggestion')
            self.assertNotIn('as it turned out', {l.candidate.expression for l in plan.lessons})

    def test_wrong_category_or_fabricated_source_never_overwrites(self):
        with TemporaryDirectory() as d:
            storage = LocalStorage(Path(d));previous = generate_plan(fixture(), storage, provider=Provider())
            for field, value in [('category', 'invalid'), ('review_id', 'invented')]:
                bad = payload(extract_candidates(fixture()));bad['items'][0][field] = value
                provider = Mock(model='mock');provider.generate.return_value = (bad, previous.usage)
                with self.assertRaises(CoachError) as error:
                    generate_plan(fixture(), storage, provider=provider, regenerate=True)
                for internal in ['出典', '候補', 'カテゴリ', 'review_id']:
                    self.assertNotIn(internal, str(error.exception))
                self.assertEqual(load_plan(storage), previous)
            bad = payload(extract_candidates(fixture()))
            old_category = bad['items'][0]['category']
            bad['items'][0]['category'] = 'weakness' if old_category != 'weakness' else 'expression'
            provider.generate.return_value = (bad, previous.usage)
            with self.assertRaises(CoachError):
                generate_plan(fixture(), storage, provider=provider, regenerate=True)
            self.assertEqual(load_plan(storage), previous)

    def test_internal_source_validation_still_checks_real_date_and_topic(self):
        with TemporaryDirectory() as d:
            storage = LocalStorage(Path(d));previous = generate_plan(fixture(), storage, provider=Provider())
            bad = extract_candidates(fixture())[0].model_copy(deep=True)
            bad.sources[0] = bad.sources[0].model_copy(update={'topic': 'Invented'})
            with patch('src.coach.service.extract_candidates', return_value=[bad]), self.assertRaises(CoachError):
                generate_plan(fixture(), storage, provider=Provider(), regenerate=True)
            self.assertEqual(load_plan(storage), previous)

    def test_prompt_changes_by_practice_level_without_word_count_padding(self):
        cs = extract_candidates(fixture())
        for level in PRACTICE_LEVELS:
            reply = Mock(status_code=200)
            reply.json.return_value = {'status':'completed', 'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(payload(cs))}]}]}
            with patch('src.coach.service.requests.post', return_value=reply) as post:
                OpenAICoach('fake', 'mock').generate(cs, practice_level=level)
            sent = post.call_args.kwargs['json'];body = json.loads(sent['input'])
            self.assertEqual(body['practice_level'], level)
            self.assertIn(PRACTICE_LEVELS[level]['instruction'], sent['instructions'])
            self.assertIn('no mandatory word count', sent['instructions'])
            self.assertNotIn('must contain 15', sent['instructions'])
            self.assertIn('NOT proof of prior learning', sent['instructions'])
            self.assertEqual(set(sent['text']['format']['schema']['$defs']['ExerciseDraft']['properties']),
                             {'candidate_id','category','situation_ja','example_en','internal_reason_ja','support_suggestions'})

    def test_legacy_plan_is_read_only_until_explicit_regeneration(self):
        with TemporaryDirectory() as d:
            storage = LocalStorage(Path(d));plan = generate_plan(fixture(), storage, provider=Provider())
            raw = plan.model_dump(mode='json');raw['schema_version']=1;raw.pop('practice_level');raw['logic_version']='2';raw['prompt_version']='2'
            for l in raw['lessons']:
                l['generated']=legacy_draft(l['candidate']['candidate_id']);l.pop('category');l.pop('support_origin')
            storage.save_json(PLAN_PATH, raw);before=storage.load_text(PLAN_PATH)
            legacy=load_plan(storage)
            self.assertEqual(legacy.practice_level, 'legacy')
            self.assertTrue(is_stale(legacy, fixture()))
            provider=Provider()
            self.assertEqual(generate_plan(fixture(), storage, provider=provider), legacy)
            self.assertEqual(provider.calls, 0)
            self.assertEqual(storage.load_text(PLAN_PATH), before)
            new=generate_plan(fixture(), storage, provider=provider, regenerate=True)
            self.assertEqual((new.logic_version,new.prompt_version),(LOGIC_VERSION,PROMPT_VERSION))


class PracticeUiTests(unittest.TestCase):
    def start(self, directory):
        from streamlit.testing.v1 import AppTest
        return AppTest.from_function(_coach_test_app, args=(directory, [r.model_dump(mode='json') for r in fixture()])).run()

    def test_compact_screen_hides_sources_reasons_and_technical_metadata(self):
        with TemporaryDirectory() as d:
            storage=LocalStorage(Path(d));plan=generate_plan(fixture(), storage, provider=Provider())
            app=self.start(d)
            self.assertEqual(len(app.exception),0)
            self.assertEqual(app.subheader[0].value,'Review Practice')
            self.assertEqual(len(app.text_area),1)
            texts=' '.join(m.value for group in [app.caption,app.markdown,app.info,app.subheader] for m in group)
            for hidden in ['Next Conversation Mission','review_id','生成方式','token','最終登場','弱点との関連','出典',plan.data_version,plan.lessons[0].generated.internal_reason_ja]:
                self.assertNotIn(hidden,texts)
            self.assertFalse(app.checkbox[0].value)
            app.checkbox[0].check().run()
            self.assertIn('例文：', ' '.join(m.value for m in app.markdown))
            app.checkbox[0].uncheck().run()
            self.assertNotIn('例文：', ' '.join(m.value for m in app.markdown))

    def test_level_switching_does_not_generate_or_write(self):
        with TemporaryDirectory() as d:
            storage=LocalStorage(Path(d));generate_plan(fixture(), storage, provider=Provider());before=storage.load_text(PLAN_PATH)
            with patch('src.coach.ui.generate_plan') as generate:
                app=self.start(d)
                self.assertEqual(app.selectbox[0].value,'応用（Applied）')
                for index in [0,2,1]:
                    app.selectbox[0].select_index(index).run()
                    self.assertEqual(len(app.exception),0)
                generate.assert_not_called()
            self.assertEqual(storage.load_text(PLAN_PATH),before)

    def test_level_mismatch_and_explicit_regeneration(self):
        from src.coach import ui
        with TemporaryDirectory() as d:
            storage=LocalStorage(Path(d));generate_plan(fixture(), storage, provider=Provider())
            app=self.start(d);app.selectbox[0].select_index(0).run()
            self.assertTrue(any('選択中：基本' in m.value and '表示中：応用' in m.value for m in app.info))
            real=generate_plan
            def regenerate(reviews, storage, **kwargs):
                return real(reviews, storage, provider=Provider(), **kwargs)
            with patch.object(ui,'generate_plan',side_effect=regenerate) as generate:
                app.button[0].click().run()
                self.assertEqual(generate.call_args.kwargs['practice_level'],'basic')
            self.assertEqual(load_plan(storage).practice_level,'basic')
            self.assertEqual(len(app.exception),0)

    def test_input_reveal_score_and_question_survive_navigation(self):
        with TemporaryDirectory() as d:
            storage=LocalStorage(Path(d));generate_plan(fixture(), storage, provider=Provider())
            app=self.start(d)
            app.text_area[0].input('My English answer').run()
            app.checkbox[0].check().run()
            app.selectbox[2].select_index(2).run()
            app.selectbox[1].select_index(1).run()
            self.assertEqual(app.text_area[0].value,'')
            self.assertFalse(app.checkbox[0].value)
            app.selectbox[1].select_index(0).run()
            self.assertEqual(app.text_area[0].value,'My English answer')
            self.assertTrue(app.checkbox[0].value)
            self.assertEqual(app.selectbox[2].value,'ヒントがあれば言えた')
            app.run()
            self.assertEqual(app.text_area[0].value,'My English answer')
