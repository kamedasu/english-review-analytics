import unittest
from unittest.mock import patch

from src.notion_client import NotionClient, blocks_to_markdown, blocks_to_review_markdown


def node(id, kind, text='', children=None):
    return {'id': id, 'type': kind, kind: {'rich_text': [{'plain_text': text}]}, 'children': children or []}


class NotionIdentityTests(unittest.TestCase):
    def test_real_heading_ids_and_nested_fields_and_rich_text(self):
        blocks = [node('a', 'heading_1', '2026-11-01 Review'),
                  node('date', 'bulleted_list_item', 'Date: 2026-11-01'),
                  node('good', 'bulleted_list_item', 'Good points:', [node('child', 'bulleted_list_item', 'Asked a question')]),
                  node('b', 'heading_1', '2026-11-01 Review'), node('topic', 'paragraph', 'Second session')]
        markdown, reviews = blocks_to_review_markdown(blocks)
        self.assertEqual([r.source_block_id for r in reviews], ['a', 'b'])
        self.assertIn('- Good points:\n  - Asked a question', reviews[0].markdown)
        self.assertEqual(markdown, blocks_to_markdown(blocks))
        self.assertNotIn('child', markdown)

    def test_heading_in_child_container_is_retained(self):
        blocks = [node('container', 'toggle', children=[node('a', 'heading_1', '2026-11-01 Review'), node('p', 'paragraph', 'Content')])]
        markdown, reviews = blocks_to_review_markdown(blocks)
        self.assertEqual(reviews[0].source_block_id, 'a')
        self.assertIn('Content', markdown)

    def test_text_or_code_cannot_forge_heading_identity(self):
        for kind in ['paragraph', 'code']:
            markdown, reviews = blocks_to_review_markdown([node('fake', kind, '# 2026-11-01 Review')])
            self.assertTrue(reviews)
            self.assertTrue(all(not r.source_block_id for r in reviews))

    def test_fake_code_header_cannot_truncate_and_update_a_real_review(self):
        _, reviews = blocks_to_review_markdown([
            node('real', 'heading_1', '2026-11-01 Review'),
            node('topic', 'paragraph', 'Actual content'),
            node('fake', 'code', '# 2026-11-02 Review'),
        ])
        self.assertEqual(len(reviews), 2)
        self.assertTrue(all(not r.source_block_id for r in reviews))

    def test_api_pagination_child_fetch_and_no_extra_id_requests(self):
        client = NotionClient('test-key')
        heading = node('h', 'heading_1', '2026-11-01 Review')
        parent = node('fields', 'bulleted_list_item', 'Good points:')
        parent['has_children'] = True
        responses = [
            {'properties': {'title': {'type': 'title', 'title': [{'plain_text': '2026-11'}]}}},
            {'results': [heading, parent], 'has_more': True, 'next_cursor': 'cursor'},
            {'results': [node('child', 'bulleted_list_item', 'Asked a question')]},
            {'results': [node('p', 'paragraph', 'More content')], 'has_more': False},
        ]
        with patch.object(client, '_request', side_effect=responses) as request:
            page = client.fetch_page_as_markdown('page')
        self.assertEqual(request.call_count, 4)
        self.assertEqual(page.title, '2026-11')
        self.assertEqual(page.review_blocks[0].source_block_id, 'h')
        self.assertIn('  - Asked a question', page.markdown)
        self.assertEqual(request.call_args.kwargs['params']['start_cursor'], 'cursor')
