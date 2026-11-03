import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from src import data_loader as loader
from src.notion_client import NotionClientError, NotionPage, NotionReviewBlock
from src.storage.base import StorageError
from src.storage.local_storage import LocalStorage
from src.storage.s3_storage import S3Storage


def page(month, page_id=None):
    markdown = f'# {month}-01 Review\n- Date: {month}-01\n- Topic: Example\n- Duration: 10'
    return NotionPage(page_id or month, month, None, markdown, (NotionReviewBlock(f'block-{page_id or month}', markdown),))


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = LocalStorage(Path(self.tmp.name))
        self.settings = SimpleNamespace(notion_api_key='test-key', notion_page_ids=[])
        self.client = Mock()
        for target, value in [('get_settings', self.settings), ('get_storage', self.storage), ('NotionClient', self.client)]:
            patcher = patch.object(loader, target, return_value=value) if target != 'NotionClient' else patch.object(loader, target, return_value=self.client)
            patcher.start()
            self.addCleanup(patcher.stop)

    def sync(self, *pages):
        self.settings.notion_page_ids = [p.page_id for p in pages]
        self.client.fetch_page_as_markdown.side_effect = list(pages)
        return loader.sync_reviews_from_notion()

    def snapshot(self):
        return {p: self.storage.load_text(p) for p in [p for prefix in ('raw', 'processed', 'state') for p in self.storage.list_files(prefix)]}

    def test_only_selected_page_and_preserve_history_and_switch(self):
        self.sync(page('2026-09'), page('2026-10'))
        before = self.snapshot()
        state_before = json.loads(before['state/state.json'])
        self.client.reset_mock()
        result = self.sync(page('2026-11'))
        self.client.fetch_page_as_markdown.assert_called_once_with('2026-11')
        self.assertEqual(len(result.reviews), 3)
        state = self.storage.load_json('state/state.json')
        for key in ('pages', 'reviews'):
            for k, v in state_before[key].items():
                self.assertEqual(state[key][k], v)
        for path, content in before.items():
            if path.startswith('raw/'):
                self.assertEqual(self.storage.load_text(path), content)
        old_reviews = [r.model_dump() for r in result.reviews]
        result = self.sync(page('2026-12'))
        self.assertEqual([r.model_dump() for r in result.reviews[:3]], old_reviews)
        self.assertEqual(len(loader.load_local_reviews().reviews), 4)

    def test_two_months(self):
        result = self.sync(page('2026-10'), page('2026-11'))
        self.assertEqual([s.synced_month for s in result.debug.page_statuses], ['2026-10', '2026-11'])

    def test_invalid_titles_abort_all_writes(self):
        self.sync(page('2026-10'))
        before = self.snapshot()
        for title in ['2026-13', '0000-01', '2026-00', '2026-1', 'prefix 2026-11', '2026-11 ', '']:
            with self.subTest(title=title):
                result = self.sync(page('2026-11'), page(title, 'invalid'))
                self.assertIn('YYYY-MM', result.debug.messages[0])
                self.assertEqual(self.snapshot(), before)

    def test_duplicate_month(self):
        result = self.sync(page('2026-11', 'a'), page('2026-11', 'b'))
        self.assertIn('複数', result.debug.messages[0])
        self.assertEqual(self.snapshot(), {})

    def test_empty_and_normal_load_never_call_notion(self):
        self.sync(page('2026-10'))
        self.client.reset_mock()
        self.settings.notion_page_ids = []
        self.settings.notion_api_key = ''
        before = self.snapshot()
        self.assertEqual(len(loader.sync_reviews_from_notion().reviews), 1)
        self.assertEqual(len(loader.load_or_fetch_reviews().reviews), 1)
        self.client.fetch_page_as_markdown.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_unchanged_review_not_reparsed(self):
        self.sync(page('2026-11'))
        with patch.object(loader, 'parse_review', wraps=loader.parse_review) as parse:
            result = self.sync(page('2026-11'))
        parse.assert_not_called()
        self.assertEqual(result.debug.page_statuses[0].skipped_count, 1)

    def test_api_failure_after_first_page_has_no_writes(self):
        self.sync(page('2026-10'))
        before = self.snapshot()
        self.settings.notion_page_ids = ['2026-11', '2026-12']
        self.client.fetch_page_as_markdown.side_effect = [page('2026-11'), NotionClientError('API unavailable')]
        result = loader.sync_reviews_from_notion()
        self.assertEqual(len(result.reviews), 1)
        self.assertEqual(self.snapshot(), before)

    def test_changed_review_updates_only_selected_page(self):
        self.sync(page('2026-10'), page('2026-11'))
        old = loader.load_local_reviews().reviews[0].model_dump()
        updated = page('2026-11')
        markdown = updated.markdown.replace('Example', 'Changed')
        updated = NotionPage(updated.page_id, updated.title, None, markdown, (NotionReviewBlock(updated.review_blocks[0].source_block_id, markdown),))
        result = self.sync(updated)
        self.assertEqual(len(result.reviews), 2)
        self.assertEqual(result.reviews[0].model_dump(), old)
        self.assertEqual(result.reviews[1].topic, 'Changed')
        self.assertEqual(result.debug.page_statuses[0].updated_count, 1)

    def test_write_failure_rolls_back(self):
        self.sync(page('2026-10'))
        before = self.snapshot()
        original = self.storage.save_text
        failed = False
        def save(path, content):
            nonlocal failed
            if path == 'state/state.json' and not failed:
                failed = True
                raise StorageError('write failed')
            original(path, content)
        with patch.object(self.storage, 'save_text', side_effect=save):
            result = self.sync(page('2026-11'))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(result.reviews), 1)
        self.assertEqual(result.debug.page_statuses[-1].status, 'エラー')

    def test_same_day_reviews_round_trip_in_storage(self):
        from tests.test_review_identity import block
        blocks = (block('first'), block('second', 'Another conversation'))
        snapshot = NotionPage('2026-11', '2026-11', None, '\n'.join(b.markdown for b in blocks), blocks)
        result = self.sync(snapshot)
        self.assertEqual(len(result.reviews), 2)
        ids = {r.review_id for r in result.reviews}
        result = self.sync(snapshot)
        self.assertEqual({r.review_id for r in result.reviews}, ids)
        self.assertEqual(result.debug.page_statuses[0].skipped_count, 2)
        persisted = loader.load_local_reviews().reviews
        self.assertEqual({r.review_id for r in persisted}, ids)
        self.assertTrue(self.storage.exists('raw/2026-11_2026-11.identity.json'))

    def test_legacy_binding_and_raw_only_candidate_stays_held(self):
        from tests.test_review_identity import block
        from src.notion_parser import parse_review
        from src.models import FetchState, ReviewState
        from src.utils.hashing import content_hash
        missing, kept = block('missing', 'First session'), block('kept', 'Second session')
        legacy = parse_review(kept.markdown, '2026-11', '2026-11')
        state = FetchState()
        for b in (missing, kept):
            r = parse_review(b.markdown, '2026-11', '2026-11')
            state.reviews[f'2026-11:{r.date}:{r.content_hash}'] = ReviewState(
                review_id=r.review_id, source_page_id='2026-11', date=r.date, content_hash=r.content_hash)
        self.storage.save_json('processed/reviews.json', [legacy.model_dump(mode='json', exclude={'source_block_id'})])
        self.storage.save_json('state/state.json', state.model_dump(mode='json'))
        markdown = '\n'.join(b.markdown for b in (missing, kept))
        self.storage.save_text('raw/2026-11_2026-11.md', markdown)
        snapshot = NotionPage('2026-11', '2026-11', None, markdown, (missing, kept))
        for _ in range(2):
            result = self.sync(snapshot)
            self.assertEqual(len(result.reviews), 1)
            self.assertEqual(result.reviews[0].review_id, legacy.review_id)
            self.assertEqual(result.reviews[0].source_block_id, 'kept')
            self.assertEqual(result.debug.page_statuses[0].held_count, 1)
            self.assertEqual(result.debug.page_statuses[0].status, '保留あり')
            self.assertEqual(self.storage.load_json('state/state.json')['reviews'], state.model_dump(mode='json')['reviews'])

    def test_failures_at_each_write_restore_raw_sidecar_processed_state(self):
        self.sync(page('2026-10'))
        before = self.snapshot()
        for fail_path in ['raw/2026-11_2026-11.md', 'raw/2026-11_2026-11.identity.json', 'processed/reviews.json', 'state/state.json']:
            with self.subTest(path=fail_path):
                original = self.storage.save_text
                failed = False
                def save(path, content):
                    nonlocal failed
                    original(path, content)
                    if path == fail_path and not failed:
                        failed = True
                        raise StorageError('failure after write')
                with patch.object(self.storage, 'save_text', side_effect=save):
                    result = self.sync(page('2026-11'))
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(len(result.reviews), 1)
                self.assertEqual(result.debug.page_statuses[-1].status, 'エラー')

    def test_unselected_legacy_row_and_state_are_unchanged(self):
        from src.notion_parser import parse_review
        from src.models import FetchState, ReviewState
        old = parse_review(page('2026-08').markdown, '2026-08', '2026-08')
        raw_row = old.model_dump(mode='json', exclude={'source_block_id'})
        state = FetchState()
        state.reviews['historical-key'] = ReviewState(review_id=old.review_id, source_page_id='2026-08', date=old.date, content_hash=old.content_hash)
        old_state = state.model_dump(mode='json')['reviews']
        self.storage.save_json('processed/reviews.json', [raw_row])
        self.storage.save_json('state/state.json', state.model_dump(mode='json'))
        self.storage.save_text('raw/2026-08_2026-08.md', old.raw_markdown)
        self.sync(page('2026-11'))
        self.assertEqual(self.storage.load_json('processed/reviews.json')[0], raw_row)
        self.assertEqual(self.storage.load_json('state/state.json')['reviews'], old_state)
        self.assertEqual(self.storage.load_text('raw/2026-08_2026-08.md'), old.raw_markdown)

    def test_no_notion_client_created_on_display_or_no_targets(self):
        with patch.object(loader, 'NotionClient') as ctor:
            loader.load_or_fetch_reviews()
            loader.sync_reviews_from_notion()
        ctor.assert_not_called()

    def test_malformed_source_mapping_aborts_without_writes(self):
        self.sync(page('2026-10'))
        before = self.snapshot()
        malformed = NotionPage('2026-11', '2026-11', None, page('2026-11').markdown)
        result = self.sync(malformed)
        self.assertEqual(result.debug.page_statuses[0].status, 'エラー')
        self.assertEqual(self.snapshot(), before)

    def test_read_only_migration_plan_and_local_to_s3_sidecars(self):
        from src.review_migration import plan_identity_migration
        from scripts.migrate_local_to_s3 import migration_files
        self.sync(page('2026-11'))
        before = self.snapshot()
        plan = plan_identity_migration(self.storage)
        self.assertFalse(plan['writes_performed'])
        self.assertEqual(plan['processed_count'], 1)
        self.assertEqual(plan['raw_only_count'], 0)
        self.assertEqual(plan['months'][0]['automatic_binding_ready'], 1)
        self.assertEqual(self.snapshot(), before)
        if isinstance(self.storage, LocalStorage):
            names = [p.name for p in migration_files(self.storage.root_dir)]
            self.assertIn('2026-11_2026-11.identity.json', names)

    def test_approved_legacy_migration_uses_storage_and_preserves_fixed_id(self):
        from src.models import FetchState
        from src.notion_parser import parse_review
        from src.review_migration import prepare_approved_bindings
        from src.utils.hashing import content_hash
        snapshot = page('2026-11')
        old = parse_review(snapshot.markdown, snapshot.page_id, snapshot.title)
        self.storage.save_json('processed/reviews.json', [old.model_dump(mode='json', exclude={'source_block_id'})])
        self.storage.save_json('state/state.json', FetchState().model_dump(mode='json'))
        self.storage.save_text('raw/2026-11_2026-11.md', snapshot.markdown)
        self.storage.save_json('raw/2026-11_2026-11.identity.json', {
            'page_id': snapshot.page_id, 'page_hash': content_hash(snapshot.markdown),
            'reviews': [{'source_block_id': snapshot.review_blocks[0].source_block_id, 'content_hash': old.content_hash}],
        })
        decisions = {
            'expected_sha256': {p: content_hash(self.storage.load_text(p)) for p in ['processed/reviews.json', 'state/state.json']},
            'bindings': [{'review_id': old.review_id, 'source_block_id': snapshot.review_blocks[0].source_block_id}],
        }
        before = self.snapshot()
        reviews, state = prepare_approved_bindings(self.storage, decisions)
        self.assertEqual(self.snapshot(), before)
        loader._save_sync(self.storage, [
            ('processed/reviews.json', json.dumps([r.model_dump(mode='json') for r in reviews])),
            ('state/state.json', state.model_dump_json()),
        ])
        saved = loader.load_local_reviews().reviews
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].review_id, old.review_id)
        self.assertEqual(saved[0].source_block_id, snapshot.review_blocks[0].source_block_id)
        self.assertEqual(self.storage.load_text('raw/2026-11_2026-11.md'), snapshot.markdown)


class ConfigTests(unittest.TestCase):
    def test_csv_ids_and_no_legacy_settings(self):
        from src.config import get_settings
        with patch.dict('os.environ', {'NOTION_PAGE_IDS': ' a, b, ,a '}):
            settings = get_settings()
        self.assertEqual(settings.notion_page_ids, ['a', 'b', 'a'])
        for field in ('notion_active_page_ids', 'active_months', 'archived_months'):
            self.assertFalse(hasattr(settings, field))


class S3SyncTests(SyncTests):
    def setUp(self):
        super().setUp()
        from botocore.exceptions import ClientError
        objects = {}
        client = Mock()
        def head(**kw):
            if kw['Key'] not in objects:
                raise ClientError({'Error': {'Code': '404'}}, 'HeadObject')
        client.head_object.side_effect = head
        client.get_object.side_effect = lambda **kw: {'Body': io.BytesIO(objects[kw['Key']])}
        client.put_object.side_effect = lambda **kw: objects.__setitem__(kw['Key'], kw['Body'])
        client.delete_object.side_effect = lambda **kw: objects.pop(kw['Key'], None)
        client.get_paginator.return_value.paginate.side_effect = lambda **kw: [{'Contents': [{'Key': k} for k in objects if k.startswith(kw['Prefix'])]}]
        with patch('src.storage.s3_storage.boto3.client', return_value=client):
            self.storage = S3Storage('test-bucket', 'data')
        patcher = patch.object(loader, 'get_storage', return_value=self.storage)
        patcher.start()
        self.addCleanup(patcher.stop)


if __name__ == '__main__':
    unittest.main()
