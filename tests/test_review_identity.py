from datetime import date
import unittest
from unittest.mock import patch

from src.models import FetchState, ReviewState
from src.notion_client import NotionReviewBlock
from src.notion_parser import parse_review
from src.review_identity import sync_review_blocks
from src.rag.document_builder import build_rag_documents
from src.rag.indexer import compute_rag_index_diff
from src.utils.hashing import content_hash


def block(source='heading-a', topic='Example', day='2026-11-01', kind='Review'):
    text = f'# {day} {kind}\n- Date: {day}\n- Duration: 15\n- Topic: {topic}\n- Good points:\n  - Asked a question'
    return NotionReviewBlock(source, text)


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.state = FetchState()
        self.reviews = []

    def sync(self, *blocks, history=None):
        result = sync_review_blocks('page', '2026-11', blocks, self.reviews, self.state, [], historical_hashes=history)
        self.reviews = result.reviews
        return result

    def test_same_day_same_type_and_reordering_and_repeat(self):
        a, b = block(), block('heading-b', 'Second conversation')
        self.assertEqual(self.sync(a, b).added_count, 2)
        original = {r.source_block_id: r.model_dump() for r in self.reviews}
        result = self.sync(b, a)
        self.assertEqual(result.skipped_count, 2)
        self.assertEqual({r.source_block_id: r.model_dump() for r in self.reviews}, original)
        self.assertEqual(self.sync(a, b).added_count, 0)
        from src.analytics import summarize_reviews
        summary = summarize_reviews(self.reviews, '2026-11')
        self.assertEqual((summary.review_count, summary.study_days, summary.total_duration_minutes), (2, 1, 30))

    def test_edit_date_type_and_a_b_a_keep_fixed_id(self):
        a = block()
        self.sync(a)
        fixed = self.reviews[0].review_id
        b = block(topic='Changed', day='2026-11-02', kind='Line English Review')
        self.assertEqual(self.sync(b).updated_count, 1)
        self.assertEqual(self.reviews[0].review_id, fixed)
        self.assertEqual(self.reviews[0].review_type, 'line')
        self.assertEqual(self.reviews[0].date, date(2026, 11, 2))
        self.assertEqual(self.sync(a).updated_count, 1)
        self.assertEqual(self.reviews[0].review_id, fixed)
        self.assertEqual(self.reviews[0].content_hash, content_hash(a.markdown))

    def test_missing_processed_is_restored_not_skipped_by_state_history(self):
        a = block()
        self.sync(a)
        original_id = self.reviews[0].review_id
        self.state.reviews[f'page:2026-11-01:{content_hash(a.markdown)}'] = self.state.current_reviews[original_id]
        self.reviews = []
        result = self.sync(a)
        self.assertEqual((result.added_count, result.skipped_count), (1, 0))
        self.assertEqual(self.reviews[0].review_id, original_id)

    def test_legacy_history_alone_cannot_skip_new_source(self):
        a = block()
        old = parse_review(a.markdown, 'page', '2026-11')
        self.state.reviews[f'page:{old.date}:{old.content_hash}'] = ReviewState(
            review_id=old.review_id, source_page_id='page', date=old.date, content_hash=old.content_hash)
        self.assertEqual(self.sync(a).added_count, 1)
        self.assertEqual(len(self.reviews), 1)

    def test_empty_heading_or_insufficient_body_keeps_existing_review(self):
        self.sync(block())
        before = self.reviews[0].model_dump()
        before_current = self.state.current_reviews.copy()
        for text in ['# 2026-11-01 Review', '# 2026-11-01 Review\n- Date: 2026-11-01\n- Topic: Example']:
            result = self.sync(NotionReviewBlock('heading-a', text))
            self.assertEqual(len(result.held), 1)
            self.assertEqual(self.reviews[0].model_dump(), before)
            self.assertEqual(self.state.current_reviews, before_current)
            self.assertEqual(self.state.pending_sources['heading-a'], 'empty_or_insufficient_review')

    def test_134_legacy_ids_and_nested_references_and_rag_ids_are_preserved(self):
        blocks = tuple(block(f'heading-{i}', f'Example {i}') for i in range(134))
        self.reviews = [parse_review(b.markdown, 'page', '2026-11') for b in blocks]
        # Exercise nested parser references too.
        from src.models import PhraseCard, MoreNaturalExpression
        for r in self.reviews:
            r.phrase_cards.append(PhraseCard(phrase='example', source_review_id=r.review_id))
            r.more_natural_expressions.append(MoreNaturalExpression(more_natural='example', source_review_id=r.review_id))
        before = [r.model_dump() for r in self.reviews]
        rag_before = build_rag_documents(self.reviews)
        result = self.sync(*blocks)
        self.assertEqual(result.added_count, 0)
        self.assertEqual(result.updated_count, 134)
        self.assertEqual(len(self.reviews), 134)
        for original, current in zip(before, self.reviews):
            original['source_block_id'] = current.source_block_id
            self.assertEqual(current.model_dump(), original)
            self.assertEqual(current.phrase_cards[0].source_review_id, current.review_id)
        diff = compute_rag_index_diff(build_rag_documents(self.reviews), rag_before)
        self.assertEqual((len(diff.added), len(diff.changed), len(diff.deleted_ids)), (0, 0, 0))

    def test_raw_only_candidate_is_held_not_automatically_restored(self):
        a = block()
        result = self.sync(a, history={content_hash(a.markdown)})
        self.assertEqual(len(result.held), 1)
        self.assertEqual(self.reviews, [])
        self.assertEqual(self.state.source_bindings, {})
        # Content changes cannot silently promote a previously held historical candidate.
        result = self.sync(block(topic='Edited historical candidate'), history={content_hash(a.markdown)})
        self.assertEqual(len(result.held), 1)
        self.assertEqual(self.reviews, [])

    def test_recreated_or_changed_unmapped_heading_is_held(self):
        self.sync(block())
        result = self.sync(block('replacement', 'Changed'))
        self.assertEqual(len(result.held), 1)
        self.assertEqual(len(self.reviews), 1)
        self.assertEqual(self.reviews[0].source_block_id, 'heading-a')

    def test_new_heading_with_original_still_present_is_independent(self):
        self.sync(block())
        result = self.sync(block(), block('second', 'Another session'))
        self.assertEqual((result.added_count, result.skipped_count), (1, 1))
        self.assertEqual(len(self.reviews), 2)

    def test_ambiguous_legacy_or_copied_hash_is_held(self):
        a = block()
        self.reviews = [parse_review(a.markdown, 'page', '2026-11')]
        result = self.sync(a, NotionReviewBlock('copy', a.markdown))
        self.assertEqual(len(result.held), 2)
        self.assertEqual(len(self.reviews), 1)
        self.assertEqual(self.state.source_bindings, {})

    def test_legacy_edited_before_binding_is_held(self):
        self.reviews = [parse_review(block().markdown, 'page', '2026-11')]
        old = self.reviews[0].model_dump()
        result = self.sync(block(topic='Changed before mapping'))
        self.assertEqual(len(result.held), 1)
        self.assertEqual(self.reviews[0].model_dump(), old)

    def test_missing_or_duplicate_source_and_parser_failure_hold(self):
        self.assertEqual(len(self.sync(block('')).held), 1)
        self.assertEqual(len(self.sync(block(), block()).held), 2)
        with patch('src.review_identity.parse_review', side_effect=ValueError('invalid')):
            # Supply a parser explicitly because the production default is captured at definition time.
            result = sync_review_blocks('page', '2026-11', (block(),), [], FetchState(), [], parser=lambda *a, **k: (_ for _ in ()).throw(ValueError('invalid')))
        self.assertEqual(result.held[0]['reason'], 'parse_failed')

    def test_conflicting_mapping_cannot_replace_another_source(self):
        self.sync(block())
        self.state.source_bindings['other'] = self.reviews[0].review_id
        result = self.sync(block('other', 'Other content'))
        self.assertEqual(len(result.held), 1)
        self.assertEqual(self.reviews[0].source_block_id, 'heading-a')

    def test_edited_review_changes_rag_under_same_document_id(self):
        self.sync(block())
        before = build_rag_documents(self.reviews)
        edited = block().markdown.replace('Asked a question', 'Asked two questions')
        self.sync(NotionReviewBlock('heading-a', edited))
        after = build_rag_documents(self.reviews)
        self.assertEqual([d.id for d in before], [d.id for d in after])
        diff = compute_rag_index_diff(after, before)
        self.assertEqual((len(diff.added), len(diff.changed), len(diff.deleted_ids)), (0, 1, 0))

    def test_rag_incremental_update_embeds_only_changed_source_and_keeps_others(self):
        from tests.test_rag_indexer import MemoryIncrementalStore, FakeEmbeddingProvider
        from src.rag.indexer import update_rag_index_incrementally
        self.sync(block(), block('b', 'Second session'))
        store = MemoryIncrementalStore(build_rag_documents(self.reviews))
        provider = FakeEmbeddingProvider()
        result = update_rag_index_incrementally(self.reviews, provider, store)
        self.assertEqual(result.embedded_count, 0)
        self.assertEqual(provider.texts, [])
        changed = NotionReviewBlock('heading-a', block().markdown.replace('Asked a question', 'Asked two questions'))
        self.sync(changed, block('b', 'Second session'))
        result = update_rag_index_incrementally(self.reviews, provider, store)
        self.assertEqual((result.added_count, result.changed_count, result.deleted_count, result.embedded_count), (0, 1, 0, 1))
        self.assertEqual(len(store.documents), 2)
        self.assertEqual(provider.texts, ['Good point: Asked two questions'])

    def test_unchanged_empty_optional_section_is_not_reparsed_forever(self):
        a = NotionReviewBlock('heading-a', block().markdown + '\n## Words and phrases actually used')
        self.sync(a)
        with patch('src.review_identity.parse_review') as parser:
            result = sync_review_blocks('page', '2026-11', (a,), self.reviews, self.state, [], parser=parser)
        parser.assert_not_called()
        self.assertEqual(result.skipped_count, 1)

    def test_future_state_version_is_rejected(self):
        self.state.identity_version = 99
        with self.assertRaises(ValueError): self.sync(block())
