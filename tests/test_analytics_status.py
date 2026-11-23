from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from src.llm_summary import MonthlySummaryResult, period_summary
from src.analytics_context import build_analysis_context
from src.storage.local_storage import LocalStorage
from src.storage.base import StorageError
from tests.test_analytics_foundation import context, response, rows, _analytics_actions_app
from tests.test_openai_model_settings import settings_env


def message(result):
    from app import analytics_status_message
    return analytics_status_message(result)


def _button_style_test_app(directory):
    from unittest.mock import patch
    import streamlit as st
    from tests.test_analytics_foundation import _analytics_actions_app
    with patch('streamlit.button',wraps=st.button) as buttons:
        _analytics_actions_app(directory)
        st.session_state['test_button_styles']=[{k:call.kwargs.get(k) for k in ('type','width')}
            for call in buttons.call_args_list if call.args[0] in ('Sync from Notion','Generate / Refresh Summary')]


class SummaryStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.storage=LocalStorage(Path(self.tmp.name));self.ctx=context()

    def test_missing_has_no_api_call(self):
        with patch('src.llm_summary.requests.post') as post:
            result=period_summary(self.ctx,self.storage)
        post.assert_not_called()
        self.assertIn('未生成',message(result))

    def test_success_and_reload_current_with_timestamp(self):
        with settings_env(),patch('src.llm_summary.requests.post',return_value=response(self.ctx)) as post:
            result=period_summary(self.ctx,self.storage,generate=True)
            self.assertEqual(message(result),'AI分析は最新です。')
            self.assertTrue(result.generated_at)
            loaded=period_summary(self.ctx,self.storage)
            self.assertEqual(result,loaded)
            self.assertEqual(post.call_count,1)

    def test_data_model_prompt_changes_need_refresh_without_api(self):
        with settings_env(),patch('src.llm_summary.requests.post',return_value=response(self.ctx)) as post:
            period_summary(self.ctx,self.storage,generate=True)
            changed={**self.ctx,'source_hash':'changed'}
            self.assertIn('更新が必要',message(period_summary(changed,self.storage)))
            with settings_env(OPENAI_MODEL='other'):
                self.assertIn('更新が必要',message(period_summary(self.ctx,self.storage)))
            with patch('src.llm_summary.SUMMARY_PROMPT_VERSION','other'):
                self.assertIn('更新が必要',message(period_summary(self.ctx,self.storage)))
            self.assertEqual(post.call_count,1)

    def test_periods_independent(self):
        with settings_env(),patch('src.llm_summary.requests.post',return_value=response(self.ctx)) as post:
            period_summary(self.ctx,self.storage,generate=True)
            for label,kind in [('2026-08','Monthly'),('2026-Q3','Quarterly'),('2026','Yearly')]:
                other=build_analysis_context(rows(),label,kind)
                self.assertIn('未生成',message(period_summary(other,self.storage)))
            self.assertEqual(post.call_count,1)

    def test_failed_refresh_retains_old_and_never_reports_latest(self):
        with settings_env(),patch('src.llm_summary.requests.post',return_value=response(self.ctx)):
            old=period_summary(self.ctx,self.storage,generate=True)
            for code in ('api_error','incomplete_response','numeric_claim'):
                failed=MonthlySummaryResult('fallback','rule-based',warning='生成失敗',error_code=code)
                with patch('src.llm_summary.generate_period_summary',return_value=failed):
                    result=period_summary(self.ctx,self.storage,generate=True)
                self.assertEqual(result.text,old.text)
                self.assertEqual(result.generated_at,old.generated_at)
                self.assertIn('更新に失敗',message(result))
                self.assertNotIn('最新',message(result))

    def test_failed_initial_generation_is_not_analyzed(self):
        with patch('src.llm_summary.generate_period_summary',return_value=MonthlySummaryResult('fallback','rule-based',error_code='api_error')):
            result=period_summary(self.ctx,self.storage,generate=True)
        self.assertIn('未生成',message(result));self.assertIn('失敗',message(result))

    def test_read_error_is_unknown_instead_of_missing(self):
        with patch.object(self.storage,'exists',side_effect=StorageError('private')),patch('src.llm_summary.requests.post') as post:
            result=period_summary(self.ctx,self.storage)
        post.assert_not_called()
        self.assertIn('状態を確認できません',message(result))
        self.assertNotIn('未生成',message(result))

    def test_save_error_retains_old_and_never_reports_latest(self):
        with settings_env(),patch('src.llm_summary.requests.post',return_value=response(self.ctx)):
            old=period_summary(self.ctx,self.storage,generate=True)
            with patch.object(self.storage,'save_json',side_effect=StorageError('private')):
                result=period_summary(self.ctx,self.storage,generate=True)
        self.assertEqual(result.text,old.text)
        self.assertIn('更新に失敗',message(result))

    def test_shared_primary_stretch_button_options(self):
        from streamlit.testing.v1 import AppTest
        app=AppTest.from_function(_button_style_test_app,args=(self.tmp.name,)).run()
        self.assertEqual(len(app.exception),0)
        self.assertEqual(app.session_state['test_button_styles'],[{'type':'primary','width':'stretch'}]*2)
        self.assertEqual([b.proto.type for b in app.sidebar.button],['primary','primary'])

    def test_ui_updates_status_on_success_and_period_change(self):
        from streamlit.testing.v1 import AppTest
        app=AppTest.from_function(_analytics_actions_app,args=(self.tmp.name,)).run()
        self.assertEqual(len(app.exception),0)
        self.assertTrue(any('AI分析は未生成' in c.value for c in app.sidebar.caption))
        app.sidebar.button[1].click().run()
        self.assertTrue(any(c.value=='AI分析は最新です。' for c in app.sidebar.caption))
        self.assertTrue(any('生成日時：' in c.value for c in app.sidebar.caption))
        app.segmented_control[0].set_value('Quarterly').run()
        self.assertTrue(any('AI分析は未生成' in c.value for c in app.sidebar.caption))
        app.segmented_control[0].set_value('Monthly').run()
        self.assertTrue(any(c.value=='AI分析は最新です。' for c in app.sidebar.caption))
        self.assertEqual(len(app.session_state['test_generated_periods']),1)
