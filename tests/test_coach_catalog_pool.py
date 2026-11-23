from collections import Counter
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import Mock, patch

from src.models import Review, PhraseCard, MoreNaturalExpression
from src.learning import build_catalog
from src.learning.models import EvidenceType as T
from src.learning.profile import LEARNER_PROFILE
from src.coach.pool import (prepare_candidate_pool, candidate_input, serialize_input, generation_input,
                            MAX_POOL_CANDIDATES, MAX_RECORDS, MAX_CANDIDATE_BYTES, MAX_INPUT_BYTES, NOTE_SNIPPET_CHARS)
from src.coach.service import CoachError, generate_plan, load_plan, PLAN_PATH, OpenAICoach, Selections
from src.coach.grading import grading_input
from src.storage.local_storage import LocalStorage
from tests.test_learning_coach import fixture, payload, Provider, legacy_draft


def diverse_reviews():
    rows=[]
    for month in range(1,9):
        for i in range(8):
            phrase=f'practice target {month} {i}'
            rows.append(Review(review_id=f'{month}:{i}',date=date(2026,month,1),topic=f'Theme {i}',
                phrase_cards=[PhraseCard(phrase=phrase)],
                more_natural_expressions=[MoreNaturalExpression(your_phrase=f'old target {month} {i}',more_natural=f'corrected target {month} {i}')],
                expressions_to_add=[f'additional target {month} {i}'],
                expressions_to_use_next_time=[f'future target {month} {i}'],
                words_and_phrases_actually_used=[f'usage target {month} {i}'],
                weak_points=[f'「weak target {month} {i}」について'],
                good_points=[f'「positive target {month} {i}」を記録']))
    return rows


class CatalogPoolTests(unittest.TestCase):
    def test_foundation_built_once_and_shared_ids_are_used(self):
        with patch('src.coach.pool.build_catalog',wraps=build_catalog) as build:
            pool=prepare_candidate_pool(fixture())
        build.assert_called_once()
        for c in pool.candidates:
            self.assertTrue(c.candidate_id.startswith('exp:v1:'))
            self.assertEqual(pool.catalog.get(c.candidate_id).expression,c.expression)
            self.assertTrue(c.sources)

    def test_source_month_and_topic_diversity_without_correction_dominance(self):
        pool=prepare_candidate_pool(diverse_reviews())
        self.assertEqual(len(pool.candidates),100)
        self.assertLessEqual(len(pool.candidates),MAX_POOL_CANDIDATES)
        origins=Counter(c.sampled_origin for c in pool.candidates)
        self.assertEqual(len(origins),7)
        self.assertLessEqual(max(origins.values())-min(origins.values()),1)
        self.assertEqual(len({c.sampled_month for c in pool.candidates}),8)
        self.assertGreater(len({c.sampled_topic for c in pool.candidates}),3)
        self.assertLess(origins['correction_record'],20)

    def test_sampling_is_deterministic_and_not_a_final_ten_selection(self):
        rows=diverse_reviews()
        first=prepare_candidate_pool(rows);second=prepare_candidate_pool(list(reversed(rows)))
        self.assertEqual([c.candidate_id for c in first.candidates],[c.candidate_id for c in second.candidates])
        self.assertGreater(len(first.candidates),10)
        self.assertEqual(len(prepare_candidate_pool(rows,limit=3).candidates),3)
        self.assertEqual(prepare_candidate_pool(rows,limit=0).candidates,[])

    def test_single_words_basics_and_different_forms_are_available(self):
        phrases=['player','each other','refresh','refreshed','refreshing','one more time','One more time, please.']
        pool=prepare_candidate_pool([Review(review_id='r',date=date(2026,1,1),phrase_cards=[PhraseCard(phrase=p) for p in phrases])])
        self.assertEqual({c.expression for c in pool.candidates},set(phrases))
        self.assertEqual(len({c.candidate_id for c in pool.candidates}),7)
        self.assertTrue(all(c.application_value==0 for c in pool.candidates))

    def test_unusable_original_and_long_context_are_not_targets_but_stay_in_catalog(self):
        row=Review(review_id='r',date=date(2026,1,1),more_natural_expressions=[
            MoreNaturalExpression(your_phrase='an incorrect original',more_natural='a recorded correction')],
            expressions_to_add=['x '*201])
        pool=prepare_candidate_pool([row]);expressions={c.expression for c in pool.candidates}
        self.assertNotIn('an incorrect original',expressions)
        self.assertIsNotNone(pool.catalog.find('an incorrect original'))
        self.assertIn('a recorded correction',expressions)
        self.assertEqual(len(pool.catalog.all()),3)
        self.assertIn('an incorrect original',pool.candidates[0].representative_records[0]['original'])

    def test_profile_b2_goal_and_self_report_do_not_change_learner_ability(self):
        pool=prepare_candidate_pool(fixture());value=generation_input(pool.candidates,'basic')
        self.assertEqual(value['learner_profile']['level'],LEARNER_PROFILE.level)
        self.assertEqual(value['learner_profile']['level_source'],'self_reported')
        self.assertEqual(value['target_practice_cefr'],'B2-oriented')
        self.assertIn('Travel',value['learner_profile']['interests'])
        self.assertEqual(value['practice_level'],'basic')
        self.assertIn('one more time',value['self_report']['comfortable_basic_expressions'])

    def test_evidence_and_input_limits_are_enforced(self):
        rows=diverse_reviews()
        for r in rows:
            r.more_natural_expressions[0].note='長い説明'*1000
        with patch('src.coach.pool.MAX_INPUT_BYTES',9000):
            pool=prepare_candidate_pool(rows)
            self.assertLessEqual(len(serialize_input(pool.candidates,'real_conversation').encode()),9000)
            self.assertLess(len(pool.candidates),100)
        for c in prepare_candidate_pool(rows).candidates:
            self.assertLessEqual(len(c.representative_records),MAX_RECORDS)
            self.assertLessEqual(len(json.dumps(candidate_input(c),ensure_ascii=False).encode()),MAX_CANDIDATE_BYTES)
            self.assertTrue(all(len(r['note'])<=NOTE_SNIPPET_CHARS for r in c.representative_records))

    def test_representatives_keep_old_new_correction_and_subsequent_usage(self):
        rows=[Review(review_id='old',date=date(2026,1,1),more_natural_expressions=[MoreNaturalExpression(more_natural='unwind',note='old')]),
              Review(review_id='new',date=date(2026,3,1),more_natural_expressions=[MoreNaturalExpression(more_natural='unwind',note='new')]),
              Review(review_id='used',date=date(2026,4,1),words_and_phrases_actually_used=['unwind'])]
        c=prepare_candidate_pool(rows).candidates[0]
        self.assertEqual([r['date'] for r in c.representative_records],['2026-03-01','2026-01-01','2026-04-01'])
        self.assertEqual(c.catalog_statistics['last_usage_date'],'2026-04-01')


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.storage=LocalStorage(Path(self.tmp.name));self.rows=diverse_reviews()
        self.pool=prepare_candidate_pool(self.rows)

    def generate(self,value,**kwargs):
        p=Mock(model='mock');p.generate.return_value=(value, {'input_tokens':3,'output_tokens':4,'total_tokens':7})
        return generate_plan(self.rows,self.storage,provider=p,**kwargs)

    def test_model_can_select_any_subset_including_low_pool_position(self):
        candidates=[self.pool.candidates[-1],self.pool.candidates[-2]]
        value=payload(candidates)
        plan=self.generate(value)
        self.assertEqual(len(plan.lessons),2)
        self.assertEqual({l.candidate.candidate_id for l in plan.lessons},{c.candidate_id for c in candidates})
        self.assertEqual(plan.schema_version,3)

    def test_related_ids_form_one_question_and_reach_existing_grading_adapter(self):
        one,two=self.pool.candidates[:2]
        value=payload([one]);item=value['items'][0]
        item['related_candidate_ids']=[two.candidate_id]
        item['source_evidence_ids'].append(two.representative_records[0]['evidence_id'])
        item['category']='expression'
        plan=self.generate(value)
        self.assertEqual(len(plan.lessons),1)
        self.assertEqual(plan.lessons[0].candidate.related_targets[0].expression,two.expression)
        self.assertEqual(len(grading_input(plan,plan.lessons[0],'My answer')['expressions']),2)

    def test_invalid_ids_duplicates_and_unrelated_evidence_preserve_old_plan(self):
        previous=self.generate(payload(self.pool.candidates));before=self.storage.load_text(PLAN_PATH)
        for mutation in [lambda v:v['items'][0].update(candidate_id='fabricated'),
                         lambda v:v['items'].append(v['items'][0]),
                         lambda v:v['items'][0].update(related_candidate_ids=['fabricated']),
                         lambda v:v['items'][0].update(related_candidate_ids=[v['items'][0]['candidate_id']]),
                         lambda v:v['items'][0].update(source_evidence_ids=['fabricated']),
                         lambda v:v['items'][0].pop('source_evidence_ids'),
                         lambda v:v['items'][0].update(source_evidence_ids=[self.pool.candidates[-1].representative_records[0]['evidence_id']]),
                         lambda v:v['items'][0].update(category='unknown'),
                         lambda v:v['items'].clear()]:
            value=payload(self.pool.candidates);mutation(value)
            with self.assertRaises(CoachError):self.generate(value,regenerate=True)
            self.assertEqual(self.storage.load_text(PLAN_PATH),before)
            self.assertEqual(load_plan(self.storage),previous)

    def test_related_candidate_cannot_reappear_in_another_question(self):
        value=payload(self.pool.candidates)
        value['items'][0]['related_candidate_ids']=[value['items'][1]['candidate_id']]
        value['items'][0]['source_evidence_ids']+=value['items'][1]['source_evidence_ids']
        with self.assertRaises(CoachError):self.generate(value)

    def test_weakness_needs_evidence_and_category_caps_are_validated(self):
        plain=[c for c in self.pool.candidates if not any(r['type'] in ('weakness_record','correction_record') for r in c.representative_records)]
        value=payload(plain[:1]);value['items'][0]['category']='weakness'
        with self.assertRaises(CoachError):self.generate(value)
        value=payload(self.pool.candidates)
        for item in value['items']:item['category']='expression'
        with self.assertRaises(CoachError):self.generate(value)
        self.assertFalse(self.storage.exists(PLAN_PATH))

    def test_more_than_ten_distinct_questions_are_rejected(self):
        value={'items':[payload([c])['items'][0] for c in self.pool.candidates[:11]]}
        with self.assertRaises(CoachError):self.generate(value)
        self.assertFalse(self.storage.exists(PLAN_PATH))

    def test_source_tampering_is_rejected(self):
        self.pool.candidates[0].sources[0]=self.pool.candidates[0].sources[0].model_copy(update={'topic':'invented'})
        with patch('src.coach.service.prepare_candidate_pool',return_value=self.pool), self.assertRaises(CoachError):
            self.generate(payload(self.pool.candidates))

    def test_one_call_and_schema_support_selection_and_safe_failures(self):
        value=payload(self.pool.candidates)
        response=Mock(status_code=200)
        response.json.return_value={'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(value)}]}]}
        with patch('src.coach.service.requests.post',return_value=response) as post:
            result,usage=OpenAICoach('fake','model').generate(self.pool.candidates)
        self.assertEqual(post.call_count,1)
        sent=post.call_args.kwargs['json'];schema=sent['text']['format']['schema']
        self.assertEqual(schema['properties']['items']['maxItems'],10)
        required=schema['$defs']['SelectionDraft']['required']
        self.assertIn('related_candidate_ids',required);self.assertIn('source_evidence_ids',required)
        self.assertNotIn('Use exactly the supplied',sent['instructions'])
        self.assertIn('SUBSET',sent['instructions']);self.assertIn('semantically redundant',sent['instructions'])
        self.assertIn('not lack of competence',sent['instructions'])
        with patch('src.coach.service.MAX_INPUT_BYTES',1), patch('src.coach.service.requests.post') as post:
            with self.assertRaises(CoachError):OpenAICoach('fake','model').generate(self.pool.candidates)
            post.assert_not_called()

    def test_legacy_short_ids_do_not_require_data_migration_or_regeneration(self):
        plan=self.generate(payload(self.pool.candidates))
        raw=plan.model_dump(mode='json');raw['schema_version']=1;raw['prompt_version']='1'
        for i,l in enumerate(raw['lessons']):
            l['candidate']['candidate_id']=f'legacy-{i}'
            l['generated']=legacy_draft(f'legacy-{i}')
        self.storage.save_json(PLAN_PATH,raw);before=self.storage.load_text(PLAN_PATH)
        with patch('src.coach.service.prepare_candidate_pool') as prepare,patch('src.coach.service.OpenAICoach') as api:
            loaded=generate_plan(self.rows,self.storage)
            self.assertEqual(loaded.lessons[0].candidate.candidate_id,'legacy-0')
            prepare.assert_not_called();api.assert_not_called()
        self.assertEqual(self.storage.load_text(PLAN_PATH),before)
