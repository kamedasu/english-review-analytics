import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src import data_loader
from src.models import FetchState
from src.notion_client import NotionPage, NotionReviewBlock
from src.review_identity import sync_review_blocks
from src.storage.local_storage import LocalStorage
from tests.test_review_identity import block


EMPTY_SOURCE = '40438d68-8a87-807f-b6fb-d9e622d2f378'
VALID_SOURCE = '40438d68-8a87-8051-8083-cbf53955ae4e'


class SyncDisplayCountsTests(unittest.TestCase):
    def test_excluded_and_unresolved_counts_preserve_original_sync_data(self):
        for unresolved in [False, True]:
            with self.subTest(unresolved=unresolved), TemporaryDirectory() as directory:
                storage = LocalStorage(Path(directory))
                valid = block(VALID_SOURCE, day='2026-10-25')
                empty = NotionReviewBlock(EMPTY_SOURCE, '# 2026-10-25 Review')
                state = FetchState(excluded_sources={EMPTY_SOURCE: 'empty_heading_2026-10-25'})
                existing = sync_review_blocks('page', '2026-10', (valid,), [], state, []).reviews
                storage.save_json('processed/reviews.json', [r.model_dump(mode='json') for r in existing])
                storage.save_json('state/state.json', state.model_dump(mode='json'))
                storage.save_text('chroma/sentinel', 'unchanged RAG')
                blocks = (valid, empty)
                if unresolved:
                    blocks += (NotionReviewBlock('unknown', '# 2026-10-26 Review'),)
                page = NotionPage('page', '2026-10', None, '\n'.join(b.markdown for b in blocks), blocks)
                settings = SimpleNamespace(notion_api_key='fake', notion_page_ids=['page'])
                client = Mock()
                client.fetch_page_as_markdown.return_value = page
                captured = []
                def sync(*args, **kwargs):
                    result = sync_review_blocks(*args, **kwargs)
                    captured.append(result)
                    return result
                with patch.object(data_loader, 'get_storage', return_value=storage), patch.object(data_loader, 'get_settings', return_value=settings), patch.object(data_loader, 'NotionClient', return_value=client), patch.object(data_loader, 'sync_review_blocks', side_effect=sync):
                    result = data_loader.sync_reviews_from_notion()
                status = result.debug.page_statuses[0]
                self.assertEqual(status.held_count, int(unresolved))
                self.assertEqual(status.excluded_count, 1)
                self.assertEqual(status.status, '保留あり' if unresolved else '同期済み')
                self.assertIn(f'保留 {int(unresolved)}, 除外済み 1', result.debug.messages[0])
                self.assertTrue(any('review除外済み (excluded_source,' in w for w in status.parser_warnings))
                self.assertFalse(any('review保留 (excluded_source,' in w for w in status.parser_warnings))
                self.assertEqual(storage.load_json('raw/2026-10_page.identity.json')['held'], captured[0].held)
                saved_state = storage.load_json('state/state.json')
                self.assertEqual(saved_state['pending_sources'][EMPTY_SOURCE], 'excluded_source')
                self.assertEqual(saved_state['excluded_sources'], {EMPTY_SOURCE: 'empty_heading_2026-10-25'})
                self.assertEqual(storage.load_json('processed/reviews.json'), [r.model_dump(mode='json') for r in existing])
                self.assertEqual(storage.load_text('chroma/sentinel'), 'unchanged RAG')
                self.assertEqual(len(captured[0].held), 1 + int(unresolved))


class SyncResultRenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Import the rendering function without launching Streamlit or doing any IO.
        with patch('streamlit.set_page_config'):
            import app
        cls.app = app

    def test_excluded_only_is_not_a_warning_and_has_separate_columns(self):
        from src.data_loader import LoadDebugInfo, PageLoadStatus
        debug = LoadDebugInfo(page_statuses=[PageLoadStatus(page_id='page', synced_month='2026-10', status='同期済み', excluded_count=1)])
        with patch.object(self.app, 'st') as st:
            self.app.render_sync_result(debug)
        st.warning.assert_not_called()
        st.info.assert_called_once()
        row = st.dataframe.call_args.args[0][0]
        self.assertEqual((row['保留'], row['除外済み']), (0, 1))

    def test_unresolved_warns_and_errors_still_take_priority(self):
        from src.data_loader import LoadDebugInfo, PageLoadStatus
        for error in [False, True]:
            with self.subTest(error=error), patch.object(self.app, 'st') as st:
                debug = LoadDebugInfo(page_statuses=[PageLoadStatus(page_id='page', status='エラー' if error else '保留あり', held_count=1, excluded_count=1)])
                self.app.render_sync_result(debug)
                if error:
                    st.error.assert_called_once()
                    st.warning.assert_not_called()
                else:
                    st.warning.assert_called_once()
                row = st.dataframe.call_args.args[0][0]
                self.assertEqual((row['保留'], row['除外済み']), (1, 1))
