"""Rotation contracts; mocked model output does not prove semantic selection quality."""
from collections import Counter
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from src.coach.pool import prepare_candidate_pool, generation_input, serialize_input
from src.coach.service import generate_plan, load_plan, CoachError, OpenAICoach, Usage
from src.storage.local_storage import LocalStorage
from tests.test_coach_catalog_pool import diverse_reviews
from tests.test_learning_coach import fixture, payload, legacy_draft


class RotationTests(unittest.TestCase):
    def test_same_generation_reproduces_ids_and_order(self):
        rows=diverse_reviews()
        for generation in (1,2,9):
            a=prepare_candidate_pool(rows,generation=generation)
            b=prepare_candidate_pool(list(reversed(rows)),generation=generation)
            self.assertEqual([c.candidate_id for c in a.candidates],[c.candidate_id for c in b.candidates])

    def test_generations_change_members_with_diversity(self):
        previous=None
        for generation in range(1,5):
            pool=prepare_candidate_pool(diverse_reviews(),generation=generation)
            ids={c.candidate_id for c in pool.candidates}
            self.assertEqual(len(ids),100)
            if previous is not None:
                self.assertTrue(ids-previous)
            previous=ids
            origins=Counter(c.sampled_origin for c in pool.candidates)
            self.assertEqual(len(origins),7)
            self.assertLessEqual(max(origins.values())-min(origins.values()),1)
            self.assertEqual(len({c.sampled_month for c in pool.candidates}),8)
            self.assertGreater(len({c.sampled_topic for c in pool.candidates}),3)

    def test_small_pool_keeps_basics_and_never_requires_replacement(self):
        a=prepare_candidate_pool(fixture(),generation=1)
        b=prepare_candidate_pool(fixture(),generation=100)
        self.assertEqual({c.candidate_id for c in a.candidates},{c.candidate_id for c in b.candidates})
        self.assertEqual(prepare_candidate_pool([],generation=4).candidates,[])
        with self.assertRaises(ValueError):
            prepare_candidate_pool([],generation=0)

    def test_history_primary_related_and_failure_generation(self):
        rows=fixture()
        def generate(candidates,**kwargs):
            return payload(candidates),Usage()
        provider=Mock(model='mock');provider.generate.side_effect=generate
        with TemporaryDirectory() as directory:
            storage=LocalStorage(Path(directory))
            first=generate_plan(rows,storage,provider=provider)
            second=generate_plan(rows,storage,regenerate=True,provider=provider)
            history=provider.generate.call_args.kwargs['previous_questions']
            self.assertEqual([q['candidate_id'] for q in history],[l.candidate.candidate_id for l in first.lessons])
            self.assertEqual(history[0]['related_candidate_ids'],first.lessons[0].generated.related_candidate_ids)
            self.assertEqual(second.generation_number,first.generation_number+1)
            # A failed request must retry the same generation and pool.
            provider.generate.side_effect=CoachError('mock failure')
            attempts=[]
            for _ in range(2):
                with self.assertRaises(CoachError):
                    generate_plan(rows,storage,regenerate=True,provider=provider)
                attempts.append([c.candidate_id for c in provider.generate.call_args.args[0]])
                self.assertEqual(load_plan(storage),second)
            self.assertEqual(*attempts)
            provider.generate.side_effect=AssertionError('Unexpected API')
            self.assertEqual(generate_plan(rows,storage,provider=provider),second)

    def test_related_ids_and_legacy_history_without_id_guessing(self):
        rows=fixture();pool=prepare_candidate_pool(rows);a,b=pool.candidates[:2]
        value=payload([a]);item=value['items'][0]
        item['related_candidate_ids']=[b.candidate_id]
        item['source_evidence_ids'].append(b.representative_records[0]['evidence_id'])
        item['category']='expression'
        provider=Mock(model='mock');provider.generate.return_value=(value,Usage())
        with TemporaryDirectory() as directory:
            storage=LocalStorage(Path(directory))
            first=generate_plan(rows,storage,provider=provider)
            generate_plan(rows,storage,provider=provider,regenerate=True)
            self.assertEqual(provider.generate.call_args.kwargs['previous_questions'][0]['related_candidate_ids'],[b.candidate_id])
            raw=first.model_dump(mode='json');raw['schema_version']=1
            raw['lessons'][0]['generated']=legacy_draft(a.candidate_id)
            storage.save_json('coach/latest.json',raw)
            generate_plan(rows,storage,provider=provider,regenerate=True)
            history=provider.generate.call_args.kwargs['previous_questions'][0]
            self.assertEqual(history['related_candidate_ids'],[])
            self.assertEqual(history['related_expressions'],[b.expression])

    def test_http_payload_carries_history_and_prioritizes_intermediate(self):
        pool=prepare_candidate_pool(fixture());value=payload(pool.candidates)
        response=Mock(status_code=200)
        response.json.return_value={'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(value)}]}]}
        history=[{'candidate_id':'not-in-pool','expression':'enjoy','related_candidate_ids':['old-id'],'related_expressions':['One more time, please.']}]
        with patch('src.coach.service.requests.post',return_value=response) as post:
            OpenAICoach('secret','mock').generate(pool.candidates,previous_questions=history)
        body=post.call_args.kwargs['json']
        self.assertEqual(json.loads(body['input'])['previous_questions'],history)
        instructions=body['instructions']
        for text in ('Prefer worthwhile intermediate expressions over plain enjoy usage','historical correction alone',
                     'BOTH the situation and example','not a prohibition','semantically equivalent',
                     'one more time / One more time, please','especially a just-practised basic'):
            self.assertIn(text,instructions)
        self.assertIn('enjoy',generation_input(pool.candidates,'applied')['self_report']['comfortable_basic_expressions'])
        self.assertEqual(post.call_count,1)
        self.assertLess(len(serialize_input(pool.candidates,'applied',history)),len(serialize_input(pool.candidates,'applied'))+1000)

    def test_invalid_output_does_not_advance_or_replace_plan(self):
        provider=Mock(model='mock');provider.generate.side_effect=lambda c,**kw:(payload(c),Usage())
        with TemporaryDirectory() as directory:
            storage=LocalStorage(Path(directory));before=generate_plan(fixture(),storage,provider=provider)
            provider.generate.side_effect=None;provider.generate.return_value=({'items':[]},Usage())
            with self.assertRaises(CoachError):
                generate_plan(fixture(),storage,regenerate=True,provider=provider)
            self.assertEqual(load_plan(storage),before)

    def test_save_failure_keeps_generation_and_same_retry_pool(self):
        from src.storage.base import StorageError
        provider=Mock(model='mock');provider.generate.side_effect=lambda c,**kw:(payload(c),Usage())
        with TemporaryDirectory() as directory:
            storage=LocalStorage(Path(directory));before=generate_plan(fixture(),storage,provider=provider)
            with patch.object(storage,'save_json',side_effect=StorageError('mock save failure')):
                with self.assertRaises(CoachError):
                    generate_plan(fixture(),storage,regenerate=True,provider=provider)
            failed_ids=[c.candidate_id for c in provider.generate.call_args.args[0]]
            self.assertEqual(load_plan(storage),before)
            after=generate_plan(fixture(),storage,regenerate=True,provider=provider)
            self.assertEqual(after.generation_number,before.generation_number+1)
            self.assertEqual(failed_ids,[c.candidate_id for c in provider.generate.call_args.args[0]])
