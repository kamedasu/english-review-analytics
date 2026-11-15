from dataclasses import FrozenInstanceError, asdict
from datetime import date
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from src.learning import (build_catalog, load_catalog, EvidenceType as T, expression_id,
                          normalize_expression, LEARNER_PROFILE)
from src.models import Review, PhraseCard, MoreNaturalExpression
from src.storage.base import Storage
from src.storage.local_storage import LocalStorage
from src.storage.s3_storage import S3Storage


def reviews():
    return [
        Review(review_id='a', date=date(2026, 1, 1), topic='Yoga Coffee', source_page_id='p', source_block_id='b',
               phrase_cards=[PhraseCard(phrase='Refresh', meaning='気分を新たにする', example='Yoga helps me refresh.',
                                       review_status='Strong', source_review_id='old-ref'), PhraseCard(phrase='refresh')],
               more_natural_expressions=[MoreNaturalExpression(your_phrase='refresh', more_natural='refreshed', note='品詞の使い分け')],
               expressions_to_add=['refreshing'], expressions_to_use_next_time=['unwind'],
               words_and_phrases_actually_used=['refresh'], weak_points=['「refresh」の使い分け', '冠詞に注意'],
               good_points=['「refreshing」を記録', '話題を広げられた'],
               my_draft=['I refreshed after yoga.'], more_natural_version=['I felt refreshed after yoga.'],
               why_it_was_corrected=['形容詞の説明'], situation='ヨガ後の会話', comment='復習用'),
        Review(review_id='b', date=date(2026, 1, 1), topic='Cafe', phrase_cards=[PhraseCard(phrase='REFRESH!')]),
        Review(review_id='c', date=date(2026, 2, 2), topic='Hiking',
               phrase_cards=[PhraseCard(phrase='refresh', meaning='別の記録', example='A different example.')],
               words_and_phrases_actually_used=['refresh'], weak_points=['「refresh」に関する注意']),
    ]


class NormalizationTests(unittest.TestCase):
    def test_surface_variants_and_idempotence(self):
        for value in ['  Ｒｅｆｒｅｓｈ  ', '“Refresh!”', "'refresh'.", 'refresh。。。', '`Refresh`', '「refresh」']:
            self.assertEqual(normalize_expression(value), 'refresh')
            self.assertEqual(expression_id(value), expression_id('refresh'))
            self.assertEqual(normalize_expression(normalize_expression(value)), 'refresh')
        self.assertEqual(normalize_expression(' I’m   going to... '), "i'm going to")
        self.assertEqual(expression_id('I’m going to'), expression_id("I'm going to"))

    def test_no_semantic_or_morphological_merging(self):
        values=['refresh','refreshed','refreshing','unwind','wind down','one more time','One more time, please.',
                "don't",'dont','in/out','in out','re-cover','recover',"dogs'",'dogs']
        self.assertEqual(len(set(expression_id(v) for v in values)),len(values))
        self.assertEqual(normalize_expression("dogs'"),"dogs'")

    def test_empty_expression_ids_rejected(self):
        for value in ['', '  ', '...', '""']:
            with self.assertRaises(ValueError): expression_id(value)


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.rows=reviews();self.catalog=build_catalog(self.rows)

    def test_all_review_fields_have_typed_records_without_invented_mistakes(self):
        counts={kind:sum(r.evidence_type==kind for r in self.catalog.evidence) for kind in T}
        self.assertEqual(counts[T.PHRASE_CARD],4)
        self.assertEqual(counts[T.CORRECTION_RECORD],1)
        self.assertEqual(counts[T.EXPLICIT_USAGE_RECORD],2)
        self.assertEqual(counts[T.WEAKNESS_RECORD],3)
        self.assertEqual(counts[T.POSITIVE_FEEDBACK],2)
        for kind in [T.SUGGESTED_EXPRESSION,T.NEXT_PRACTICE_EXPRESSION,T.DRAFT_RECORD,T.CORRECTED_VERSION_RECORD,T.CORRECTION_EXPLANATION]:
            self.assertEqual(counts[kind],1)
        self.assertEqual(counts[T.CONTEXT_RECORD],2)
        self.assertFalse(any('mistake' in r.evidence_type for r in self.catalog.evidence))

    def test_correction_one_record_both_sides_no_usage_claim(self):
        record=next(r for r in self.catalog.evidence if r.evidence_type==T.CORRECTION_RECORD)
        self.assertEqual(record.original_text,'refresh');self.assertEqual(record.corrected_text,'refreshed')
        self.assertEqual(record.note,'品詞の使い分け');self.assertEqual(len(record.expression_ids),2)
        self.assertEqual({link.role for link in record.links},{'original','corrected'})
        stats=self.catalog.find('refreshed').statistics
        self.assertEqual(stats.correction_record_count,1)
        self.assertEqual(stats.explicit_usage_record_count,0)
        self.assertEqual(stats.last_usage_date,None)

    def test_before_after_same_normalized_expression_counts_once(self):
        c=build_catalog([Review(review_id='same',date=date(2026,1,1),
                more_natural_expressions=[MoreNaturalExpression(your_phrase='Refresh',more_natural='refresh!')])])
        self.assertEqual(len(c.evidence[0].links),2)
        self.assertEqual(c.find('refresh').statistics.correction_record_count,1)

    def test_unique_reviews_different_from_records_or_dates(self):
        stats=self.catalog.find('refresh').statistics
        self.assertEqual(stats.unique_review_count,3)
        self.assertEqual(stats.record_count,9)
        self.assertEqual(stats.learning_record_count,4)
        self.assertEqual(stats.explicit_usage_record_count,2)
        self.assertEqual(stats.weakness_record_count,2)
        self.assertEqual(self.catalog.find('refresh').occurrence_count,3)

    def test_original_variants_metadata_and_source_references_preserved(self):
        item=self.catalog.find('refresh')
        self.assertEqual(set(item.original_variants),{'Refresh','refresh','REFRESH!'})
        self.assertEqual(item.meanings,('別の記録','気分を新たにする'))
        record=next(r for r in self.catalog.evidence if r.source_field=='phrase_cards' and r.review_id=='a')
        self.assertEqual((record.source_page_id,record.source_block_id),('p','b'))
        self.assertEqual(dict(record.source_metadata)['review_status'],'Strong')
        self.assertEqual(dict(record.source_metadata)['source_review_id'],'old-ref')
        self.assertFalse(hasattr(item,'mastered'))
        self.assertFalse(hasattr(item,'difficulty'))

    def test_unstructured_records_stay_review_level_even_in_english(self):
        c=build_catalog([Review(review_id='r',date=date(2026,1,1),
             weak_points=['Use articles carefully.', '時制に注意'],good_points=['Natural conversation.'])])
        self.assertEqual(c.all(),())
        self.assertEqual(len(c.unlinked_evidence),3)
        self.assertTrue(all(r.extraction_issue=='no_unambiguous_expression' for r in c.evidence))

    def test_mixed_language_only_explicit_quotes_are_extracted(self):
        c=build_catalog([Review(review_id='r',date=date(2026,1,1),
             expressions_to_add=['「unwind」：くつろぐ', 'refresh：気分を変える'],
             weak_points=['“refresh” と 「refreshed」の使い分け', "'that's why'の使い方"])])
        self.assertIsNotNone(c.find('unwind'));self.assertIsNotNone(c.find("that's why"))
        self.assertEqual(c.find('refresh').statistics.weakness_record_count,1)
        self.assertEqual(c.find('refreshed').statistics.weakness_record_count,1)
        self.assertEqual(len(c.unlinked_evidence),1)
        self.assertEqual(c.evidence[0].review_id,'r')

    def test_duplicate_items_preserved_and_ids_reproducible(self):
        row=Review(review_id='r',date=date(2026,1,1),words_and_phrases_actually_used=['refresh','refresh'])
        c=build_catalog([row])
        self.assertEqual(len(c.evidence),2)
        self.assertEqual(len(set(r.evidence_id for r in c.evidence)),2)
        self.assertEqual(c.find('refresh').statistics.explicit_usage_record_count,2)
        self.assertEqual(c.find('refresh').occurrence_count,1)
        self.assertEqual(c.evidence,build_catalog([row]).evidence)

    def test_period_statistics_rebuilt_inclusive_and_no_future_metadata(self):
        key=expression_id('refresh')
        jan=self.catalog.select(start_date=date(2026,1,1),end_date=date(2026,1,31))
        item=jan.get(key)
        self.assertEqual(item.statistics.unique_review_count,2)
        self.assertEqual(item.statistics.record_count,6)
        self.assertEqual(item.meanings,('気分を新たにする',))
        self.assertNotIn('Hiking',item.topics)
        feb=self.catalog.statistics(key,start_date=date(2026,2,1))
        self.assertEqual(feb.unique_review_count,1)
        self.assertEqual(feb.record_count,3)
        self.assertEqual(feb.correction_record_count,0)
        self.assertIsNone(feb.last_correction_date)
        empty=self.catalog.statistics(key,start_date=date(2027,1,1))
        self.assertEqual(empty.record_count,0);self.assertIsNone(empty.first_seen)
        self.assertEqual(len(self.catalog.history(key,end_date=date(2026,1,1))),6)

    def test_topic_and_evidence_filters_reaggregate(self):
        view=self.catalog.select(topic='COFFEE',evidence_types=[T.EXPLICIT_USAGE_RECORD])
        self.assertEqual(len(view.evidence),1)
        self.assertEqual(view.find('refresh').statistics.record_count,1)
        self.assertEqual(view.find('refresh').statistics.learning_record_count,0)
        self.assertEqual(view.find('refresh').statistics.unique_review_count,1)
        self.assertEqual(self.catalog.select(evidence_types=[]).all(),())
        self.assertEqual(self.catalog.select(topic='nonexistent').all(),())
        self.assertEqual(len(self.catalog.select(evidence_types=['weakness_record']).unlinked_evidence),1)

    def test_last_dates_and_usage_queries_scoped_to_evidence(self):
        item=self.catalog.find('refresh');key=item.expression_id
        self.assertIn(key,[e.expression_id for e in self.catalog.search(last_usage_after=date(2026,2,1))])
        self.assertEqual(self.catalog.search(last_correction_after=date(2026,2,1)),())
        self.assertIn(key,[e.expression_id for e in self.catalog.search(last_correction_before=date(2026,1,1))])
        self.assertNotIn(key,[e.expression_id for e in self.catalog.search(has_usage=False)])
        self.assertEqual(self.catalog.search(start_date=date(2026,2,1),last_correction_before=date(2026,2,5)),())
        self.assertEqual(self.catalog.search(last_seen_after=date(2027,1,1)),())
        self.assertIn(key,[e.expression_id for e in self.catalog.search(last_seen_before=date(2026,1,31),end_date=date(2026,1,31))])

    def test_invalid_filter_ranges_and_unknown_kinds(self):
        for call in [lambda:self.catalog.select(start_date=date(2027,1,1),end_date=date(2026,1,1)),
                     lambda:self.catalog.history('unknown',start_date=date(2027,1,1),end_date=date(2026,1,1)),
                     lambda:self.catalog.search(last_usage_after=date(2027,1,1),last_usage_before=date(2026,1,1)),
                     lambda:self.catalog.select(evidence_types=['mistake'])]:
            with self.assertRaises(ValueError):call()
        self.assertIsNone(self.catalog.get('unknown'));self.assertIsNone(self.catalog.find('...'))

    def test_rebuild_detects_additions_edits_deletions_without_mutating_input(self):
        before=[r.model_dump(mode='json') for r in self.rows]
        original_hash=self.catalog.source_data_hash
        row=self.rows[0].model_copy(deep=True);row.phrase_cards[0].meaning='変更'
        changed=build_catalog([row,*self.rows[1:]])
        self.assertNotEqual(original_hash,changed.source_data_hash)
        self.assertEqual(changed.find('refresh').expression_id,self.catalog.find('refresh').expression_id)
        self.assertNotEqual(original_hash,build_catalog(self.rows[:1]).source_data_hash)
        self.assertNotEqual(original_hash,build_catalog([*self.rows,Review(review_id='new',date=date(2026,1,2))]).source_data_hash)
        self.assertEqual(before,[r.model_dump(mode='json') for r in self.rows])

    def test_determinism_review_order_and_immutable_results(self):
        reverse=build_catalog(reversed(self.rows))
        self.assertEqual(self.catalog.all(),reverse.all())
        self.assertEqual(self.catalog.evidence,reverse.evidence)
        self.assertEqual(self.catalog.source_data_hash,reverse.source_data_hash)
        with self.assertRaises(FrozenInstanceError):self.catalog.all()[0].expression='changed'
        with self.assertRaises(FrozenInstanceError):self.catalog.evidence[0].original_text='changed'

    def test_empty_old_models_and_empty_records(self):
        self.assertEqual(build_catalog([]).all(),())
        old=build_catalog([{'review_id':'old','date':'2026-01-01','phrase_cards':[{'phrase':'refresh'}]}])
        self.assertIsNotNone(old.find('refresh'))
        c=build_catalog([Review(review_id='r',date=date(2026,1,1),phrase_cards=[PhraseCard()],weak_points=[''])])
        self.assertEqual(len(c.unlinked_evidence),2)
        self.assertTrue(all(r.extraction_issue=='empty_record' for r in c.evidence))
        with self.assertRaises(ValueError):build_catalog([self.rows[0],self.rows[0]])

    def test_fullwidth_english_not_discarded(self):
        c=build_catalog([Review(review_id='r',date=date(2026,1,1),expressions_to_add=['Ｒｅｆｒｅｓｈｅｄ'])])
        self.assertIsNotNone(c.find('refreshed'))
        self.assertEqual(c.find('refreshed').original_variants,('Ｒｅｆｒｅｓｈｅｄ',))

    def test_common_profile_compatible_level_separate_from_task_difficulty(self):
        from src.coach.profile import LEARNER_PROFILE as coach
        self.assertEqual((LEARNER_PROFILE.level,LEARNER_PROFILE.cefr),(coach.level,coach.cefr))
        self.assertEqual(LEARNER_PROFILE.level_source,'self_reported')
        self.assertIn('Travel',LEARNER_PROFILE.interests)
        self.assertFalse(hasattr(LEARNER_PROFILE,'practice_level'))


class CatalogStorageTests(unittest.TestCase):
    def test_local_read_only_loader(self):
        with TemporaryDirectory() as d:
            storage=LocalStorage(Path(d))
            self.assertEqual(load_catalog(storage).all(),())
            storage.save_json('processed/reviews.json',[r.model_dump(mode='json') for r in reviews()])
            before=storage.load_text('processed/reviews.json')
            with patch.object(storage,'save_text',side_effect=AssertionError('Read only')):
                c=load_catalog(storage)
            self.assertEqual(len(c.evidence),len(build_catalog(reviews()).evidence))
            self.assertEqual(storage.load_text('processed/reviews.json'),before)
            self.assertEqual(storage.list_files('learning'),[])

    def test_mock_s3_read_only_loader_same_result(self):
        import json
        client=Mock()
        content=json.dumps([r.model_dump(mode='json') for r in reviews()]).encode()
        client.get_object.return_value={'Body':io.BytesIO(content)}
        with patch('src.storage.s3_storage.boto3.client',return_value=client):
            storage=S3Storage('test-foundation','data')
            c=load_catalog(storage)
        self.assertEqual(c.all(),build_catalog(reviews()).all())
        client.get_object.assert_called_once_with(Bucket='test-foundation',Key='data/processed/reviews.json')
        client.put_object.assert_not_called();client.delete_object.assert_not_called()

    def test_loader_rejects_malformed_shape(self):
        storage=Mock(spec=Storage);storage.exists.return_value=True;storage.load_json.return_value={}
        with self.assertRaises(ValueError):load_catalog(storage)
        storage.save_json.assert_not_called()


class EvidenceBoundaryTests(unittest.TestCase):
    def test_no_quality_filter_and_no_semantic_merge(self):
        phrases=['player','each other','refresh','refreshed','refreshing','one more time','One more time, please.']
        c=build_catalog([Review(review_id='r',date=date(2026,1,1),phrase_cards=[PhraseCard(phrase=p) for p in phrases])])
        self.assertEqual(len(c.all()),len(phrases))
        self.assertEqual(len({c.find(p).expression_id for p in phrases}),len(phrases))
        self.assertTrue(all(c.find(p).statistics.explicit_usage_record_count==0 for p in phrases))

    def test_quoted_correction_note_is_explicit_link_not_inferred_error(self):
        row=Review(review_id='r',date=date(2026,1,1),more_natural_expressions=[
            MoreNaturalExpression(your_phrase='I feel good.',more_natural='I feel great.',
                                  note='「refreshing」は別の説明。使えないという意味ではない。')])
        c=build_catalog([row]);item=c.find('refreshing')
        self.assertIsNotNone(item)
        self.assertEqual([l.role for l in c.evidence[0].links if l.expression_id==item.expression_id],['note_quoted'])
        self.assertEqual(item.statistics.correction_record_count,1)
        self.assertEqual(item.statistics.weakness_record_count,0)
        self.assertEqual(item.statistics.explicit_usage_record_count,0)
        self.assertFalse(hasattr(c.evidence[0],'is_mistake'))
