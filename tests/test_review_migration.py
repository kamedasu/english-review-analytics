import contextlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from src.models import FetchState
from src.notion_parser import parse_review
from src.review_migration import plan_identity_migration, prepare_approved_bindings
from src.storage.local_storage import LocalStorage
from src.storage.state_store import update_page_state
from src.utils.hashing import content_hash
from scripts import migrate_review_identity
from tests.test_review_identity import block


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.storage = LocalStorage(self.root)
        self.a = block()
        self.review = parse_review(self.a.markdown, 'page', '2026-11')
        self.storage.save_json('processed/reviews.json', [self.review.model_dump(mode='json', exclude={'source_block_id'})])
        state = FetchState()
        update_page_state(state, 'page', '2026-11', None, content_hash(self.a.markdown))
        self.storage.save_json('state/state.json', state.model_dump(mode='json'))
        self.storage.save_text('raw/2026-11_page.md', self.a.markdown)
        self.storage.save_json('raw/2026-11_page.identity.json', {
            'page_id': 'page', 'page_hash': content_hash(self.a.markdown),
            'reviews': [{'source_block_id': self.a.source_block_id, 'content_hash': content_hash(self.a.markdown)}]})
        self.decisions = {
            'expected_sha256': {p: content_hash(self.storage.load_text(p)) for p in ('processed/reviews.json', 'state/state.json')},
            'bindings': [{'review_id': self.review.review_id, 'source_block_id': self.a.source_block_id}],
        }

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def test_plan_and_prepare_are_read_only_and_keep_existing_id(self):
        before = self.snapshot()
        plan = plan_identity_migration(self.storage)
        reviews, state = prepare_approved_bindings(self.storage, self.decisions)
        self.assertEqual(plan['months'][0]['automatic_binding_ready'], 1)
        self.assertEqual(reviews[0].review_id, self.review.review_id)
        self.assertEqual(reviews[0].source_block_id, self.a.source_block_id)
        self.assertEqual(state.source_bindings[self.a.source_block_id], self.review.review_id)
        self.assertEqual(self.snapshot(), before)

    def test_atomic_local_replace_failure_does_not_truncate_existing_file(self):
        from src.storage.base import StorageError
        before = self.snapshot()
        with patch('src.storage.local_storage.os.replace', side_effect=OSError('replace failed')):
            with self.assertRaises(StorageError):
                self.storage.save_text('processed/reviews.json', 'partial replacement')
        self.assertEqual(self.snapshot(), before)

    def test_apply_runs_only_on_explicit_fixture_with_hash_validation(self):
        decisions_file = self.root/'reviewed.json'
        decisions_file.write_text(json.dumps(self.decisions))
        before = self.snapshot()
        args = ['migrate', '--data-root', str(self.root), '--bindings', str(decisions_file)]
        with patch('sys.argv', args), contextlib.redirect_stdout(io.StringIO()):
            migrate_review_identity.main()
        self.assertEqual(self.snapshot(), before)
        with patch('sys.argv', args + ['--apply']), contextlib.redirect_stdout(io.StringIO()):
            migrate_review_identity.main()
        persisted = self.storage.load_json('processed/reviews.json')
        self.assertEqual(persisted[0]['review_id'], self.review.review_id)
        self.assertEqual(persisted[0]['source_block_id'], self.a.source_block_id)
        after = self.snapshot()
        with self.assertRaises(ValueError):
            prepare_approved_bindings(self.storage, self.decisions)
        self.assertEqual(self.snapshot(), after)

    def test_unapproved_new_review_or_duplicate_binding_or_changed_mapping_is_rejected(self):
        for edit in ['new', 'duplicate', 'mapping', 'input']:
            with self.subTest(edit=edit):
                decisions = json.loads(json.dumps(self.decisions))
                if edit == 'new': decisions['bindings'][0]['review_id'] = 'not-an-existing-review'
                if edit == 'duplicate': decisions['bindings'] *= 2
                if edit == 'mapping': decisions['bindings'][0]['source_block_id'] = 'unknown-block'
                if edit == 'input': decisions['expected_sha256']['state/state.json'] = 'old-hash'
                before = self.snapshot()
                with self.assertRaises(ValueError): prepare_approved_bindings(self.storage, decisions)
                self.assertEqual(self.snapshot(), before)

    def test_raw_only_and_empty_candidates_reported_without_restoration(self):
        missing = block('b', 'Another session')
        empty = '# 2026-11-03 Review'
        markdown = self.a.markdown + '\n' + missing.markdown + '\n' + empty
        self.storage.save_text('raw/2026-11_page.md', markdown)
        before = self.snapshot()
        plan = plan_identity_migration(self.storage)
        self.assertEqual(plan['raw_count'], 3)
        self.assertEqual(plan['processed_count'], 1)
        self.assertEqual(plan['raw_only_count'], 2)
        self.assertEqual({c['reason'] for c in plan['raw_only_candidates']}, {'independence_confirmation_required', 'empty_or_insufficient_review'})
        self.assertFalse(plan['months'][0]['source_mapping_available'])
        self.assertEqual(self.snapshot(), before)
