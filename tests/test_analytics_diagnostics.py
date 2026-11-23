"""Mock-only regression coverage for prose quantities and sanitized diagnostics."""
from datetime import date
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import requests
from src.analytics_context import build_analysis_context
from src.models import Review, PhraseCard
from src.llm_summary import (AnalysisReport, SummaryFailure, _render_report, generate_period_summary,
                             period_summary, _validate_prose_numbers)
from src.storage.local_storage import LocalStorage
from src.storage.base import StorageError
from tests.test_analytics_foundation import context, report, response
from tests.test_openai_model_settings import settings_env

LOGGER='src.llm_summary'


class ProseQuantityTests(unittest.TestCase):
    def test_normal_japanese_level_labels_and_methods_are_allowed(self):
        ctx=context()
        for text in ('十分な証拠がありません','十分に練習しましょう','証拠が十分です','学習時間は十分でした',
                     'B1レベル','B2レベル','B1〜B2','5W1H','一度確認しましょう','もう一度練習しましょう'):
            with self.subTest(text=text):
                value=report(ctx);value['observations'][0]['interpretation_ja']=text
                self.assertIn(text,_render_report(AnalysisReport.model_validate(value),ctx))

    def test_explicit_statistical_claims_are_rejected(self):
        for text in ('レビュー数は999件でした','10月の学習時間は9,999分でした','前月比150%増加しました',
                     'レビュー数は９９９件でした','三回使えました','学習時間は十分間でした',
                     '学習時間は九千分でした','2026-11-01に改善しました','前年比一五〇パーセント増でした'):
            with self.subTest(text=text), self.assertRaises(SummaryFailure) as error:
                _validate_prose_numbers(text)
            self.assertEqual(error.exception.code,'numeric_claim')

    def test_python_renders_authoritative_values_even_if_prose_matches(self):
        ctx=context()
        self.assertIn('35分',_render_report(AnalysisReport.model_validate(report(ctx)),ctx))
        # Matching quantities still belong to Python, not generated prose.
        with self.assertRaises(SummaryFailure): _validate_prose_numbers('学習時間は35分でした')

    def test_november_only_with_no_comparison_succeeds(self):
        rows=[Review(review_id='nov',date=date(2026,11,1),duration_minutes=15,
                     phrase_cards=[PhraseCard(phrase='wind down')])]
        ctx=build_analysis_context(rows,'2026-11',today=date(2026,11,23))
        value=report(ctx);value['observations'][0]['interpretation_ja']='習得を判断するには十分な証拠がありません。B1〜B2の表現を確認しましょう。'
        reply=response(ctx);reply.json.return_value={'status':'completed','output_text':json.dumps(value)}
        with settings_env(),patch('src.llm_summary.requests.post',return_value=reply):
            result=generate_period_summary(None,[],context=ctx)
        self.assertEqual(result.source,'llm');self.assertEqual(result.error_code,'')
        self.assertIn('比較対象の記録なし',result.text)
        self.assertIn('15分',result.text)


class DiagnosticTests(unittest.TestCase):
    def run_failure(self,reply,expected):
        with settings_env(),patch('src.llm_summary.requests.post',return_value=reply),self.assertLogs(LOGGER,level='WARNING') as logs:
            result=generate_period_summary(None,[],context=context())
        self.assertEqual(result.source,'rule-based')
        self.assertEqual(result.error_code,expected)
        self.assertIn('reason='+expected,'\n'.join(logs.output))
        self.assertTrue(result.warning)
        return result,'\n'.join(logs.output)

    def test_http_api_failure_is_distinct_and_safe(self):
        reply=response(context(),400);reply.json.return_value={'error':{'message':'SECRET_KEY PRIVATE_REVIEW'}}
        result,logs=self.run_failure(reply,'api_error')
        self.assertIn('status=400',logs)
        self.assertNotIn('SECRET_KEY',logs+result.warning)
        self.assertNotIn('PRIVATE_REVIEW',logs+result.warning)

    def test_incomplete_response_is_distinct(self):
        reply=response(context());reply.json.return_value={'status':'incomplete','incomplete_details':{'reason':'PRIVATE'}}
        self.run_failure(reply,'incomplete_response')

    def test_structured_json_parse_failure_is_distinct(self):
        reply=response(context());reply.json.return_value={'status':'completed','output_text':'PRIVATE invalid json'}
        result,logs=self.run_failure(reply,'parse_error')
        self.assertNotIn('PRIVATE',logs+result.warning)

    def test_schema_violation_is_distinct(self):
        reply=response(context());reply.json.return_value={'status':'completed','output_text':json.dumps({'observations':[{'PRIVATE':'SECRET'}]})}
        result,logs=self.run_failure(reply,'schema_error')
        self.assertNotIn('PRIVATE',logs+result.warning)
        self.assertNotIn('SECRET',logs+result.warning)

    def test_unknown_evidence_is_distinct(self):
        ctx=context();value=report(ctx);value['observations'][0]['evidence_ids']=['PRIVATE_UNKNOWN_ID']
        reply=response(ctx);reply.json.return_value={'status':'completed','output_text':json.dumps(value)}
        result,logs=self.run_failure(reply,'unknown_evidence')
        self.assertNotIn('PRIVATE_UNKNOWN_ID',logs+result.warning)

    def test_numeric_validation_is_distinct(self):
        ctx=context();value=report(ctx);value['observations'][0]['interpretation_ja']='PRIVATEレビュー数は999件でした'
        reply=response(ctx);reply.json.return_value={'status':'completed','output_text':json.dumps(value)}
        result,logs=self.run_failure(reply,'numeric_claim')
        self.assertNotIn('999',logs+result.warning)
        self.assertNotIn('PRIVATE',logs+result.warning)

    def test_transport_exception_message_not_logged(self):
        with settings_env(),patch('src.llm_summary.requests.post',side_effect=requests.Timeout('test-key PRIVATE_BODY')), \
             self.assertLogs(LOGGER,level='WARNING') as logs:
            result=generate_period_summary(None,[],context=context())
        text='\n'.join(logs.output)+result.warning
        self.assertEqual(result.error_code,'api_error')
        self.assertIn('error_type=Timeout',text)
        self.assertNotIn('test-key',text);self.assertNotIn('PRIVATE_BODY',text)

    def test_cache_save_failure_retains_summary_and_reports_stage(self):
        ctx=context()
        with TemporaryDirectory() as directory,settings_env(),patch('src.llm_summary.requests.post',return_value=response(ctx)):
            storage=LocalStorage(Path(directory));before=period_summary(ctx,storage,generate=True)
            original={p:p.read_bytes() for p in Path(directory).rglob('*') if p.is_file()}
            with patch.object(storage,'save_json',side_effect=StorageError('PRIVATE secret')), \
                 self.assertLogs(LOGGER,level='WARNING') as logs:
                result=period_summary(ctx,storage,generate=True)
            self.assertEqual(result.text,before.text)
            self.assertEqual(result.error_code,'cache_save')
            self.assertIn('stage=cache_save','\n'.join(logs.output))
            self.assertNotIn('PRIVATE','\n'.join(logs.output)+result.warning)
            self.assertEqual(original,{p:p.read_bytes() for p in Path(directory).rglob('*') if p.is_file()})

    def test_generation_failure_preserves_previous_with_error_code(self):
        ctx=context()
        with TemporaryDirectory() as directory,settings_env(),patch('src.llm_summary.requests.post',return_value=response(ctx)):
            storage=LocalStorage(Path(directory));before=period_summary(ctx,storage,generate=True)
            original=storage.load_text('analytics/summaries/Monthly_2026-09.json')
            with patch('src.llm_summary.requests.post',return_value=response(ctx,403)):
                result=period_summary(ctx,storage,generate=True)
            self.assertEqual(result.text,before.text);self.assertEqual(result.error_code,'api_error')
            self.assertEqual(original,storage.load_text('analytics/summaries/Monthly_2026-09.json'))
