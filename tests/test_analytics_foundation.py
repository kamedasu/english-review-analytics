from collections import Counter
from datetime import date
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import requests
from src.analytics import summarize_reviews, summarize_month
from src.analytics_context import (build_analysis_context, period_bounds, comparison, encode,
                                   MAX_CONTEXT_BYTES, MAX_EVIDENCE, context_fingerprint)
from src.learning import build_catalog
from src.models import Review, PhraseCard, MoreNaturalExpression
from src.llm_summary import (period_summary, generate_period_summary, facts_only, _summary_instructions,
                            _render_report, AnalysisReport, MonthlySummaryResult, SUMMARY_PROMPT_VERSION)
from src.storage.local_storage import LocalStorage
from src.storage.s3_storage import S3Storage
from src.storage.base import StorageError
from tests.test_openai_model_settings import settings_env


def rows():
    return [Review(review_id='old',date=date(2026,7,1),duration_minutes=10,topic='Nature',
                   weak_points=['Use "unwind" naturally'],
                   more_natural_expressions=[MoreNaturalExpression(your_phrase='I relax',more_natural='I unwind',note='Optional paraphrase')]),
            Review(review_id='prev',date=date(2026,8,2),topic='Coffee',
                   phrase_cards=[PhraseCard(phrase='unwind')],weak_points=['Use "unwind" naturally']),
            Review(review_id='new',date=date(2026,9,2),duration_minutes=20,topic='Friends',
                   phrase_cards=[PhraseCard(phrase='unwind'),PhraseCard(phrase='unwind')],
                   words_and_phrases_actually_used=['unwind'],
                   more_natural_expressions=[MoreNaturalExpression(your_phrase='very nice',more_natural='refreshing',note='Style suggestion')]),
            Review(review_id='later',date=date(2026,9,3),duration_minutes=15,topic='Nature',
                   phrase_cards=[PhraseCard(phrase='unwind'),PhraseCard(phrase='wind down')],
                   weak_points=['A new issue'])]


def context():
    return build_analysis_context(rows(),'2026-09',today=date(2026,11,23))


def report(ctx):
    return {'observations':[{'section':'themes','assessment':'proposal',
            'interpretation_ja':'記録した表現を場面に合わせて使い分ける練習を提案します。',
            'evidence_ids':[ctx['evidence'][0]['evidence_id']]}]}


def response(ctx,status=200):
    value=Mock(status_code=status)
    value.json.return_value={'status':'completed','output_text':json.dumps(report(ctx))}
    return value


class ContextTests(unittest.TestCase):
    def test_calendar_bounds_rollover_leap_quarters_years(self):
        self.assertEqual(period_bounds('2024-02','Monthly'),(date(2024,2,1),date(2024,2,29),date(2024,1,1),date(2024,1,31)))
        self.assertEqual(period_bounds('2026-01','Monthly')[2:],(date(2025,12,1),date(2025,12,31)))
        self.assertEqual(period_bounds('2026-Q1','Quarterly'),(date(2026,1,1),date(2026,3,31),date(2025,10,1),date(2025,12,31)))
        self.assertEqual(period_bounds('2026','Yearly'),(date(2026,1,1),date(2026,12,31),date(2025,1,1),date(2025,12,31)))
        for label,kind in [('2026-Q5','Quarterly'),('2026-13','Monthly')]:
            with self.assertRaises(ValueError): period_bounds(label,kind)

    def test_one_foundation_and_exact_record_statistics(self):
        with patch('src.analytics_context.build_catalog',wraps=build_catalog) as build:
            ctx=context()
        build.assert_called_once()
        current=ctx['facts']['current']
        self.assertEqual((current['study_minutes'],current['reviews'],current['study_days']),(35,2,2))
        self.assertEqual((current['phrase_card_records'],current['explicit_usage_records'],current['correction_records'],current['weakness_records']),(4,1,1,1))
        self.assertEqual(current['reappearing_expressions'],1)
        self.assertEqual(ctx['facts']['comparison']['study_minutes']['percent'],None)
        self.assertEqual(ctx['facts']['comparison']['study_minutes']['delta'],35)
        self.assertEqual(current['phrase_card_records'],summarize_month(rows(),'2026-09').phrase_count)
        self.assertEqual(current['legacy_reappearance_matches'],summarize_month(rows(),'2026-09').reused_phrase_count)

    def test_quarter_year_scopes_and_missing_previous(self):
        for label,kind in [('2026-Q3','Quarterly'),('2026','Yearly')]:
            ctx=build_analysis_context(rows(),label,kind,today=date(2026,12,31))
            self.assertEqual(ctx['facts']['current']['reviews'],4)
            self.assertFalse(ctx['facts']['previous']['has_records'])
            self.assertIsNone(ctx['facts']['comparison']['reviews']['delta'])
            self.assertIn('比較対象の記録なし',facts_only(ctx))

    def test_future_excluded_and_partial_period_explicit(self):
        ctx=build_analysis_context(rows(),'2026-09',today=date(2026,9,2))
        self.assertEqual(ctx['facts']['current']['reviews'],1)
        self.assertEqual(ctx['facts']['current']['end'],'2026-09-02')
        self.assertFalse(ctx['facts']['complete_period'])
        self.assertFalse(ctx['facts']['equal_observed_days'])
        self.assertTrue(all(r['date']<='2026-09-02' for r in ctx['evidence']))
        with self.assertRaises(ValueError): build_analysis_context(rows(),'2026-10',today=date(2026,9,1))

    def test_history_first_last_and_later_usage_not_mastery(self):
        ctx=context();weak=next(r for r in ctx['evidence'] if r['original']=='Use "unwind" naturally')
        self.assertEqual((weak['first_recorded'],weak['last_recorded']),('2026-07-01','2026-08-02'))
        self.assertEqual((weak['current_records'],weak['previous_records']),(0,1))
        self.assertEqual(weak['later_usage_dates'],['2026-09-02'])
        self.assertIn('not proof',weak['followup_basis'])
        self.assertNotIn('mastered',weak)

    def test_reappearance_is_not_usage_and_card_count_not_unique(self):
        r=rows();r[2].words_and_phrases_actually_used=[]
        c=build_analysis_context(r,'2026-09',today=date(2026,12,31))['facts']['current']
        self.assertEqual(c['explicit_usage_records'],0)
        self.assertEqual(c['reappearing_expressions'],1)
        self.assertEqual(c['phrase_card_records'],4)
        self.assertGreater(c['unique_expressions'],0)

    def test_context_bounded_deduplicated_diverse_and_no_raw(self):
        from tests.test_coach_catalog_pool import diverse_reviews
        source=diverse_reviews()
        for r in source:
            r.raw_markdown='PRIVATE RAW'
            r.more_natural_expressions[0].note='長い説明'*500
        ctx=build_analysis_context(source,'2026','Yearly',today=date(2026,12,31))
        self.assertLessEqual(len(encode(ctx).encode()),MAX_CONTEXT_BYTES)
        self.assertLessEqual(len(ctx['evidence']),MAX_EVIDENCE)
        self.assertNotIn('PRIVATE RAW',encode(ctx))
        self.assertGreater(len({r['type'] for r in ctx['evidence']}),3)
        self.assertGreater(len({r['date'][:7] for r in ctx['evidence']}),1)
        self.assertTrue(any(r['clipped'] for r in ctx['evidence']))
        signatures=[(r['type'],r['original'],r['corrected'],r['note']) for r in ctx['evidence']]
        self.assertEqual(len(signatures),len(set(signatures)))

    def test_source_hash_includes_unselected_change(self):
        a=context();source=rows();source[0].good_points=['Additional evidence']
        b=build_analysis_context(source,'2026-09',today=date(2026,11,23))
        self.assertNotEqual(a['source_hash'],b['source_hash'])
        self.assertNotEqual(context_fingerprint(a,'model','v'),context_fingerprint(b,'model','v'))

    def test_semantic_judgment_delegated_and_fallback_no_fabricated_drill(self):
        text=_summary_instructions('Monthly')
        for phrase in ['意味的に同種','More Natural Expressions','正用','採点履歴','最近の指摘がないだけでは改善']:
            self.assertIn(phrase,text)
        fallback=facts_only(context())
        self.assertIn('意味分類・成長評価は行っていません',fallback)
        self.assertIn('AI Learning Coach',fallback)
        self.assertNotIn('set aside',fallback)
        self.assertNotIn('定着寄り',fallback)


class ReportTests(unittest.TestCase):
    def test_verified_evidence_and_python_only_numbers(self):
        ctx=context();text=_render_report(AnalysisReport.model_validate(report(ctx)),ctx)
        self.assertIn('35分',text)
        self.assertIn('AIの解釈',text)
        self.assertIn(ctx['evidence'][0]['date'],text)
        for replacement in ['学習時間は999分です。','2026年に改善しました。','三回使えました。']:
            value=report(ctx);value['observations'][0]['interpretation_ja']=replacement
            with self.assertRaises(ValueError): _render_report(AnalysisReport.model_validate(value),ctx)

    def test_unknown_evidence_and_wrong_usage_rejected(self):
        ctx=context();value=report(ctx);value['observations'][0]['evidence_ids']=['invented']
        with self.assertRaises(ValueError): _render_report(AnalysisReport.model_validate(value),ctx)
        value=report(ctx);value['observations'][0]['section']='usage'
        value['observations'][0]['evidence_ids']=[next(r['evidence_id'] for r in ctx['evidence'] if r['type']=='explicit_usage_record')]
        with self.assertRaises(ValueError): _render_report(AnalysisReport.model_validate(value),ctx)

    def test_mock_api_schema_model_privacy_and_closed_response(self):
        ctx=context();reply=response(ctx)
        with settings_env(OPENAI_MODEL='configured'),patch('src.llm_summary.requests.post',return_value=reply) as post:
            result=generate_period_summary(None,[],context=ctx)
        self.assertEqual(result.source,'llm')
        body=post.call_args.kwargs['json']
        self.assertEqual(body['model'],'configured')
        self.assertEqual(body['text']['format']['type'],'json_schema')
        self.assertFalse(body['store'])
        self.assertEqual(body['input'],encode(ctx))
        self.assertEqual(post.call_count,1)
        reply.close.assert_called_once()

    def test_errors_invalid_json_refusal_timeout_are_safe(self):
        ctx=context()
        replies=[response(ctx,404),response(ctx),response(ctx),response(ctx)]
        replies[1].json.return_value={'status':'incomplete'}
        replies[2].json.return_value={'status':'completed','output_text':'bad json'}
        replies[3].json.return_value={'status':'completed','output':[{'type':'message','content':[{'type':'refusal'}]}]}
        with settings_env():
            for reply in replies:
                with patch('src.llm_summary.requests.post',return_value=reply):
                    result=generate_period_summary(None,[],context=ctx)
                    self.assertEqual(result.source,'rule-based')
                    self.assertTrue(result.warning)
                    self.assertNotIn('test-key',result.warning)
            with patch('src.llm_summary.requests.post',side_effect=requests.Timeout('PRIVATE test-key')):
                self.assertNotIn('PRIVATE',generate_period_summary(None,[],context=ctx).warning)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.storage=LocalStorage(Path(self.tmp.name));self.ctx=context()

    def test_display_never_calls_api_and_explicit_generation_saved(self):
        with settings_env(),patch('src.llm_summary.requests.post',return_value=response(self.ctx)) as post:
            first=period_summary(self.ctx,self.storage)
            self.assertEqual(first.source,'rule-based');post.assert_not_called()
            generated=period_summary(self.ctx,self.storage,generate=True)
            self.assertEqual(generated.source,'llm');self.assertEqual(post.call_count,1)
            self.assertEqual(period_summary(self.ctx,self.storage),generated)
            self.assertEqual(post.call_count,1)

    def test_stale_model_input_prompt_period_no_automatic_call(self):
        with settings_env(),patch('src.llm_summary.requests.post',return_value=response(self.ctx)) as post:
            period_summary(self.ctx,self.storage,generate=True)
            with patch('src.llm_summary.SUMMARY_PROMPT_VERSION','new'):
                self.assertTrue(period_summary(self.ctx,self.storage).stale)
            changed=json.loads(json.dumps(self.ctx));changed['source_hash']='different'
            self.assertTrue(period_summary(changed,self.storage).stale)
            with settings_env(OPENAI_MODEL='different'):
                self.assertTrue(period_summary(self.ctx,self.storage).stale)
            other=build_analysis_context(rows(),'2026-08',today=date(2026,11,23))
            self.assertEqual(period_summary(other,self.storage).source,'rule-based')
            self.assertEqual(post.call_count,1)

    def test_api_failure_and_save_failure_preserve_successful_cache(self):
        with settings_env(),patch('src.llm_summary.requests.post',return_value=response(self.ctx)):
            saved=period_summary(self.ctx,self.storage,generate=True)
            before={p:p.read_bytes() for p in Path(self.tmp.name).rglob('*') if p.is_file()}
            with patch('src.llm_summary.requests.post',side_effect=requests.Timeout('private')):
                result=period_summary(self.ctx,self.storage,generate=True)
                self.assertEqual(result.text,saved.text);self.assertTrue(result.warning)
            with patch.object(self.storage,'save_json',side_effect=StorageError('private')):
                result=period_summary(self.ctx,self.storage,generate=True)
                self.assertEqual(result.text,saved.text);self.assertTrue(result.warning)
            self.assertEqual(before,{p:p.read_bytes() for p in Path(self.tmp.name).rglob('*') if p.is_file()})

    def test_no_api_key_facts_only_and_no_saved_fallback(self):
        with settings_env(OPENAI_API_KEY=''),patch('src.llm_summary.requests.post') as post:
            result=period_summary(self.ctx,self.storage,generate=True)
            post.assert_not_called();self.assertEqual(result.source,'rule-based')
            self.assertFalse(list(Path(self.tmp.name).rglob('*.json')))

    def test_mock_s3_round_trip_without_network(self):
        from botocore.exceptions import ClientError
        objects={};client=Mock()
        def head(**kw):
            if kw['Key'] not in objects: raise ClientError({'Error':{'Code':'404'}},'HeadObject')
            return {}
        client.head_object.side_effect=head
        client.get_object.side_effect=lambda **kw:{'Body':io.BytesIO(objects[kw['Key']])}
        client.put_object.side_effect=lambda **kw:objects.__setitem__(kw['Key'],kw['Body'])
        with patch('src.storage.s3_storage.boto3.client',return_value=client):
            storage=S3Storage('test-analytics')
        with settings_env(),patch('src.llm_summary.requests.post',return_value=response(self.ctx)) as post:
            generated=period_summary(self.ctx,storage,generate=True)
            self.assertEqual(period_summary(self.ctx,storage),generated)
            self.assertEqual(post.call_count,1)
        self.assertEqual(len(objects),1)


def _analytics_test_app(directory):
    from pathlib import Path
    from unittest.mock import patch
    import app
    from src.storage.local_storage import LocalStorage
    from tests.test_analytics_foundation import rows
    from src.llm_summary import MonthlySummaryResult
    with patch.object(app,'get_storage',return_value=LocalStorage(Path(directory))), \
         patch('src.llm_summary.generate_period_summary',return_value=MonthlySummaryResult('AI sentinel','llm','mock')) as generate:
        app.render_analytics(rows())
        import streamlit as st
        st.session_state['test_api_calls']=st.session_state.get('test_api_calls',0)+generate.call_count


class AnalyticsUITests(unittest.TestCase):
    def test_api_only_on_explicit_button_and_reuse_on_redraw(self):
        from streamlit.testing.v1 import AppTest
        with TemporaryDirectory() as directory:
            app=AppTest.from_function(_analytics_test_app,args=(directory,)).run()
            self.assertEqual(len(app.exception),0)
            self.assertEqual(app.session_state['test_api_calls'],0)
            self.assertIn('Phrase Card Records',[m.label for m in app.metric])
            self.assertIn('Reappearance Matches',[m.label for m in app.metric])
            app.button[0].click().run()
            self.assertEqual(len(app.exception),0)
            self.assertEqual(app.session_state['test_api_calls'],1)
            app.run()
            self.assertEqual(app.session_state['test_api_calls'],1)
            self.assertIn('AI sentinel',[m.value for m in app.markdown])

    def test_period_switch_does_not_generate(self):
        from streamlit.testing.v1 import AppTest
        with TemporaryDirectory() as directory:
            app=AppTest.from_function(_analytics_test_app,args=(directory,)).run()
            for kind in ('Quarterly','Yearly','Monthly'):
                app.segmented_control[0].set_value(kind).run()
                self.assertEqual(len(app.exception),0)
                self.assertEqual(app.session_state['test_api_calls'],0)


def _analytics_actions_app(directory):
    from pathlib import Path
    from unittest.mock import patch
    import streamlit as st
    import app
    from src.data_loader import LoadResult, LoadDebugInfo
    from src.storage.local_storage import LocalStorage
    from src.llm_summary import MonthlySummaryResult
    from tests.test_analytics_foundation import rows
    result=LoadResult(rows(),LoadDebugInfo())
    with patch.object(app,'cached_load_reviews',return_value=result), \
         patch.object(app,'load_or_fetch_reviews',return_value=result), \
         patch.object(app,'get_storage',return_value=LocalStorage(Path(directory))), \
         patch('src.llm_summary.generate_period_summary',return_value=MonthlySummaryResult('AI sentinel','llm','mock')) as generate:
        app.main()
        if generate.called:
            ctx=generate.call_args.kwargs['context']
            st.session_state['test_generated_periods']=st.session_state.get('test_generated_periods',[])+[(ctx['period_type'],ctx['period'])]


class AnalyticsActionPlacementTests(unittest.TestCase):
    def test_sidebar_order_sync_does_not_generate_and_selected_period_only(self):
        from streamlit.testing.v1 import AppTest
        with TemporaryDirectory() as directory:
            app=AppTest.from_function(_analytics_actions_app,args=(directory,)).run()
            self.assertEqual(len(app.exception),0)
            self.assertEqual([b.label for b in app.sidebar.button],['Sync from Notion','Generate / Refresh Summary'])
            self.assertTrue(any('分析対象：Monthly / 2026-09' in c.value for c in app.sidebar.caption))
            app.sidebar.button[0].click().run()
            self.assertEqual(len(app.exception),0)
            self.assertNotIn('test_generated_periods',app.session_state)
            for kind,label in [('Monthly','2026-09'),('Quarterly','2026-Q3'),('Yearly','2026')]:
                app.segmented_control[0].set_value(kind).run()
                app.sidebar.button[1].click().run()
                self.assertEqual(len(app.exception),0)
                self.assertEqual(app.session_state['test_generated_periods'][-1],(kind,label))
            before=list(app.session_state['test_generated_periods'])
            app.run()
            self.assertEqual(app.session_state['test_generated_periods'],before)
