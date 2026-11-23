"""Contract/local evaluation cases, not evidence that an actual LLM judges well."""
from datetime import date
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from src.models import Review, PhraseCard, MoreNaturalExpression
from src.coach.pool import (prepare_candidate_pool, candidate_input, serialize_input,
                            MAX_RECORDS, TEXT_SNIPPET_CHARS, NOTE_SNIPPET_CHARS)
from src.coach.service import OpenAICoach, generate_plan, Selections
from src.storage.local_storage import LocalStorage


TARGETS = ['enjoy','offer to do',"I'm going to",'one more time','One more time, please.',
           'Could you say that again?','soak in','make room for','run into',
           "I've got a window",'give off a cozy vibe','refresh','refreshed','refreshing']


def quality_reviews():
    return [
        Review(review_id='learned',date=date(2026,1,1),topic='Yoga and cafes',
               phrase_cards=[PhraseCard(phrase=x) for x in TARGETS]),
        Review(review_id='grammar',date=date(2026,2,1),more_natural_expressions=[MoreNaturalExpression(
            your_phrase='I enjoy to hike.',more_natural='I enjoy hiking.',note='「enjoy」の後は動名詞。')]),
        Review(review_id='optional',date=date(2026,3,1),more_natural_expressions=[MoreNaturalExpression(
            your_phrase='I like quiet cafes.',more_natural='I enjoy quiet cafes.',
            note='「enjoy」も可能。必須の文法修正ではなく任意の言い換え。')]),
        Review(review_id='weak',date=date(2026,4,1),weak_points=['「enjoy」の後の動名詞を確認する']),
        Review(review_id='usage',date=date(2026,5,1),words_and_phrases_actually_used=['enjoy']),
    ]


def draft(candidate, *, related=(), category='expression', situation='友人に最近の出来事を自然に伝えてください。',
          example='I would like to make room for a little more relaxation this weekend.'):
    candidates=[candidate,*related]
    ids=list(dict.fromkeys(c.representative_records[0]['evidence_id'] for c in candidates))
    return {'candidate_id':candidate.candidate_id,'category':category,'situation_ja':situation,
            'example_en':example,'internal_reason_ja':'記録された表現を状況に合わせて使う練習です。',
            'support_suggestions':[],'related_candidate_ids':[c.candidate_id for c in related],
            'source_evidence_ids':ids}


class EvidenceQualityTests(unittest.TestCase):
    def test_plain_basic_target_and_explicit_grammar_issue_are_distinguishable(self):
        plain=prepare_candidate_pool(quality_reviews()[:1]);c=next(c for c in plain.candidates if c.expression=='enjoy')
        self.assertEqual(c.catalog_statistics['correction_record_count'],0)
        self.assertEqual(c.catalog_statistics['weakness_record_count'],0)
        rich=prepare_candidate_pool(quality_reviews());c=next(c for c in rich.candidates if c.expression=='enjoy')
        records=c.representative_records
        grammar=next(r for r in records if r['review_id']=='grammar')
        self.assertEqual(grammar['original'],'I enjoy to hike.')
        self.assertEqual(grammar['corrected'],'I enjoy hiking.')
        self.assertIn('動名詞',grammar['note'])
        self.assertEqual(grammar['pair_status'],'both_sides_different')
        self.assertTrue(any(r['type']=='weakness_record' for r in records))
        self.assertTrue(any(r['type']=='explicit_usage_record' for r in records))
        self.assertEqual(len(records),4)

    def test_optional_paraphrase_and_note_role_do_not_become_python_error_labels(self):
        pool=prepare_candidate_pool(quality_reviews())
        c=next(c for c in pool.candidates if c.expression=='enjoy')
        record=next(r for r in c.representative_records if r['review_id']=='optional')
        self.assertEqual(record['roles'],['note_quoted'])
        self.assertIn('任意の言い換え',record['note'])
        self.assertNotIn('mistake',record)
        self.assertNotIn('is_grammar_error',record)
        self.assertNotIn('current_weakness',candidate_input(c))
        usage=next(r for r in c.representative_records if r['type']=='explicit_usage_record')
        self.assertGreater(usage['date'],record['date'])

    def test_missing_original_is_flagged_and_never_invented(self):
        pool=prepare_candidate_pool([Review(review_id='r',date=date(2026,1,1),
             more_natural_expressions=[MoreNaturalExpression(more_natural='make room for')])])
        record=pool.candidates[0].representative_records[0]
        self.assertEqual(record['original'],'')
        self.assertEqual(record['pair_status'],'corrected_only')

    def test_target_centered_excerpts_keep_late_explanation_with_clipping_flags(self):
        long_note='前置きの説明。'*70+'「enjoy」の後は動名詞。'
        row=Review(review_id='r',date=date(2026,1,1),more_natural_expressions=[MoreNaturalExpression(
            your_phrase='A long introduction. '*25+'I enjoy to hike.',
            more_natural='A long introduction. '*25+'I enjoy hiking.',note=long_note)])
        pool=prepare_candidate_pool([row]);c=next(c for c in pool.candidates if c.expression=='enjoy')
        r=c.representative_records[0]
        self.assertIn('enjoy to hike',r['original']);self.assertIn('enjoy hiking',r['corrected'])
        self.assertIn('動名詞',r['note'])
        self.assertEqual(set(r['clipped_fields']),{'original','corrected','note'})
        self.assertLessEqual(len(r['note']),NOTE_SNIPPET_CHARS)
        self.assertLessEqual(len(r['original']),TEXT_SNIPPET_CHARS)
        self.assertEqual(row.more_natural_expressions[0].note,long_note)

    def test_internal_provenance_is_retained_without_redundant_model_metadata(self):
        pool=prepare_candidate_pool(quality_reviews());c=pool.candidates[0]
        self.assertIn('review_id',c.representative_records[0])
        sent=candidate_input(c)['records'][0]
        self.assertIn('evidence_id',sent);self.assertIn('date',sent);self.assertIn('roles',sent)
        for key in ['review_id','source_field','source_index','topic']:
            self.assertNotIn(key,sent)
        self.assertLess(len(json.dumps(sent)),len(json.dumps(c.representative_records[0])))

    def test_all_named_evaluation_targets_remain_available_without_word_count_ranking(self):
        p=prepare_candidate_pool(quality_reviews())
        self.assertTrue(set(TARGETS).issubset({c.expression for c in p.candidates}))
        self.assertTrue(all(c.application_value==0 for c in p.candidates))
        self.assertTrue(all(len(c.representative_records)<=MAX_RECORDS for c in p.candidates))


class GenerationQualityContractTests(unittest.TestCase):
    def setUp(self):
        self.rows=quality_reviews();self.pool=prepare_candidate_pool(self.rows)
        self.by_expression={c.expression:c for c in self.pool.candidates}

    def request(self,level='applied',items=None):
        response=Mock(status_code=200)
        response.json.return_value={'status':'completed','output':[{'type':'message','content':[
            {'type':'output_text','text':json.dumps({'items':items or [draft(self.pool.candidates[0])]})}]}]}
        with patch('src.coach.service.requests.post',return_value=response) as post:
            OpenAICoach('fake','model').generate(self.pool.candidates,practice_level=level)
        self.assertEqual(post.call_count,1)
        return post.call_args.kwargs['json']

    def test_prompt_compares_specific_evidence_instead_of_frequency_or_basic_word_names(self):
        prompt=self.request()['instructions']
        for phrase in ['grammar/usage correction','optional paraphrases','mere appearances',
                       'More Natural Expressions alone never proves a mistake','Down-rank a basic target with no specific issue',
                       'enjoy doing','past record, not proof of a current weakness','note_quoted','clipped_fields']:
            self.assertIn(phrase,prompt)
        self.assertIn('Do not blacklist basic targets',prompt)

    def test_expression_and_task_difficulty_are_separate_and_levels_conditional(self):
        for level in ['basic','applied','real_conversation']:
            sent=self.request(level);prompt=sent['instructions']
            self.assertEqual(json.loads(sent['input'])['practice_level'],level)
            for phrase in ['TWO independent axes','basic expression + meaningful applied contrast',
                           'obscure expression','not assign a fixed difficulty','Do not make every answer long',
                           'interlocutor reaction','declining help','no mandatory word count']:
                self.assertIn(phrase,prompt)

    def test_semantic_duplicates_and_contrasting_forms_have_different_instructions(self):
        prompt=self.request()['instructions']
        self.assertIn('Could you say that again',prompt)
        self.assertIn('semantically redundant learning goals',prompt)
        self.assertIn('refresh/refreshed/refreshing',prompt)
        self.assertIn('Never repeat a primary or related ID',prompt)

    def test_existing_structured_output_contract_is_unchanged(self):
        schema=self.request()['text']['format']['schema']
        fields=set(schema['$defs']['SelectionDraft']['properties'])
        self.assertEqual(fields,{'candidate_id','category','situation_ja','example_en','internal_reason_ja',
                                 'support_suggestions','related_candidate_ids','source_evidence_ids'})
        self.assertEqual(schema,Selections.model_json_schema())
        self.assertEqual(schema['properties']['items']['maxItems'],10)

    def test_mock_simple_and_applied_offer_tasks_use_same_target_and_validation(self):
        candidate=self.by_expression['offer to do']
        examples=[('友人に手伝いを申し出たことを伝えてください。','I offered to help my friend.'),
                  ('ヨガイベントの準備を手伝うと申し出たところ相手は遠慮しました。無理に押しつけず、必要になったら声をかけてほしいと伝えてください。',
                   'I offered to help with the setup, but she said she had everything under control, so I told her to let me know if anything came up.')]
        with TemporaryDirectory() as d:
            storage=LocalStorage(Path(d))
            for situation,example in examples:
                provider=Mock(model='mock');provider.generate.return_value=({'items':[draft(candidate,situation=situation,example=example)]},{})
                plan=generate_plan(self.rows,storage,provider=provider,regenerate=True)
                self.assertEqual(plan.lessons[0].candidate.candidate_id,candidate.candidate_id)
                self.assertEqual(plan.lessons[0].generated.example_en,example)
                self.assertEqual(provider.generate.call_count,1)
        # These fixtures test transport/validation, not the relative semantic quality of real model responses.

    def test_mock_one_question_can_group_duplicates_or_contrasting_forms(self):
        groups=[['one more time','One more time, please.','Could you say that again?'],
                ['refresh','refreshed','refreshing']]
        with TemporaryDirectory() as d:
            storage=LocalStorage(Path(d))
            for names in groups:
                candidates=[self.by_expression[name] for name in names]
                provider=Mock(model='mock');provider.generate.return_value=({'items':[draft(candidates[0],related=candidates[1:])]},{})
                plan=generate_plan(self.rows,storage,provider=provider,regenerate=True)
                self.assertEqual(len(plan.lessons),1)
                self.assertEqual(len(plan.lessons[0].candidate.related_targets),2)
                self.assertTrue(plan.lessons[0].candidate.practice_evidence)
