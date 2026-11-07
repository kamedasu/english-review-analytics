import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests

from src.notion_client import NotionClient, NotionClientError, _RequestLimiter
from src import data_loader
from src.rag.sync_update import run_rag_update_after_sync
from src.storage.local_storage import LocalStorage


class Clock:
    def __init__(self):
        self.now = 100.0
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


def response(status=200, body=None, header=None):
    r = Mock(status_code=status, headers={} if header is None else {'Retry-After': header})
    r.json.return_value = {} if body is None else body
    r.text = 'PRIVATE-BODY test-secret'
    return r


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.limiter = _RequestLimiter(clock=self.clock, sleep=self.clock.sleep)
        self.session = Mock()
        self.starts = []

    def client(self, responses, **kwargs):
        queue = iter(responses)
        def request(*args, **kw):
            self.starts.append(self.clock())
            item = next(queue)
            if isinstance(item, Exception):
                raise item
            return item
        self.session.request.side_effect = request
        return NotionClient('test-secret', limiter=self.limiter, session=self.session,
                            jitter=lambda: .25, **kwargs)

    def test_all_requests_and_clients_share_interval(self):
        client = self.client([response(), response(), response()])
        other = NotionClient('other-key', limiter=self.limiter, session=self.session)
        client._request('GET', '/pages/a')
        other._request('GET', '/blocks/a/children')
        client._request('GET', '/blocks/b/children', {'start_cursor': 'next'})
        self.assertEqual(self.starts, [100, 100.5, 101])
        self.assertEqual(self.session.request.call_args.kwargs['allow_redirects'], False)

    def test_default_clients_share_limiter_and_no_adapter_retries(self):
        a, b = NotionClient('a'), NotionClient('b')
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        self.assertIs(a.limiter, b.limiter)
        self.assertEqual(a.session.adapters['https://'].max_retries.total, 0)

    def test_429_then_success_respects_header_and_repeats_only_request(self):
        limited = response(429, header='65')
        notice = []
        client = self.client([limited, response(body={'ok': True})], on_retry=notice.append)
        self.assertEqual(client._request('GET', '/pages/a'), {'ok': True})
        self.assertEqual(self.starts, [100, 165])
        self.assertEqual(client.retry_wait, 65)
        self.assertEqual(self.session.request.call_args_list[0], self.session.request.call_args_list[1])
        self.assertEqual(len(notice), 1)
        limited.close.assert_called_once()

    def test_long_header_aborts_without_sleep_but_shared_cooldown_remains(self):
        client = self.client([response(429, header='121')])
        with self.assertRaisesRegex(NotionClientError, 'wait_budget'):
            client._request('GET', '/pages/a')
        self.assertEqual(self.clock.waits, [])
        self.assertEqual(self.session.request.call_count, 1)
        self.assertEqual(self.limiter.blocked_until, 221)
        other = NotionClient('b', limiter=self.limiter, session=self.session)
        with self.assertRaisesRegex(NotionClientError, 'wait_budget'):
            other._request('GET', '/pages/b')
        self.assertEqual(self.session.request.call_count, 1)

    def test_missing_or_invalid_header_backoff_and_jitter(self):
        for header in [None, '', 'bad', '-1', 'NaN', 'Infinity', '0.5']:
            with self.subTest(header=header):
                self.setUp()
                c = self.client([response(429, header=header), response(429, header=header), response()])
                c._request('GET', '/pages/a')
                self.assertEqual(self.clock.waits, [1.25, 2.25])

    def test_repeated_429_stops_at_retry_limit(self):
        c = self.client([response(429, header='1') for _ in range(3)], max_retries=2)
        with self.assertRaisesRegex(NotionClientError, 'retry_exhausted'):
            c._request('GET', '/pages/a')
        self.assertEqual(self.session.request.call_count, 3)
        self.assertEqual(self.clock.waits, [1, 1])

    def test_cumulative_wait_budget_across_page_requests(self):
        c = self.client([response(429, header='70'), response(), response(429, header='60')])
        c._request('GET', '/pages/a')
        with self.assertRaisesRegex(NotionClientError, 'wait_budget'):
            c._request('GET', '/pages/b')
        self.assertEqual(self.clock.waits, [70, .5])
        self.assertEqual(self.session.request.call_count, 3)

    def test_shared_cooldown_applies_after_exhaustion(self):
        c = self.client([response(429, header='3'), response()], max_retries=0)
        with self.assertRaises(NotionClientError):
            c._request('GET', '/pages/a')
        other = NotionClient('b', limiter=self.limiter, session=self.session)
        other._request('GET', '/pages/b')
        self.assertEqual(self.starts, [100, 103])

    def test_child_and_pagination_retries_do_not_duplicate_results(self):
        c = self.client([
            response(body={'properties': {}}),
            response(body={'results': [{'id': 'parent', 'has_children': True}], 'has_more': True, 'next_cursor': 'next'}),
            response(429, header='2'),
            response(body={'results': [{'id': 'child'}]}),
            response(429, header='1'),
            response(body={'results': [{'id': 'last'}]}),
        ])
        blocks = c._request('GET', '/pages/a')
        blocks = c._fetch_all_blocks('a')
        self.assertEqual([b['id'] for b in blocks], ['parent', 'last'])
        self.assertEqual([b['id'] for b in blocks[0]['children']], ['child'])
        calls = self.session.request.call_args_list
        self.assertEqual(calls[2], calls[3])
        self.assertEqual(calls[4], calls[5])
        self.assertTrue(all(b-a >= .5 for a, b in zip(self.starts, self.starts[1:])))

    def test_missing_and_repeated_cursors_abort(self):
        for cursor in [None, '', 'same']:
            with self.subTest(cursor=cursor):
                self.setUp()
                c = self.client([response(body={'results': [], 'has_more': True, 'next_cursor': 'same'}),
                                 response(body={'results': [], 'has_more': True, 'next_cursor': cursor})])
                with self.assertRaisesRegex(NotionClientError, 'cursor'):
                    c._fetch_all_blocks('a')
                self.assertEqual(self.session.request.call_count, 2)

    def test_non_retryable_status_and_transport_errors_never_leak_secrets(self):
        cases = [response(401), response(403), response(404), response(500), response(503),
                 requests.Timeout('PRIVATE-BODY test-secret'), requests.ConnectionError('PRIVATE-BODY test-secret')]
        for item in cases:
            with self.subTest(item=type(item).__name__):
                self.setUp()
                c = self.client([item])
                with self.assertLogs('src.notion_client', level=logging.INFO) as logs:
                    with self.assertRaises(NotionClientError) as error:
                        c._request('GET', '/pages/private')
                text = '\n'.join(logs.output) + str(error.exception)
                for secret in ['test-secret', 'PRIVATE-BODY', '/pages/private', 'Authorization']:
                    self.assertNotIn(secret, text)
                self.assertEqual(self.session.request.call_count, 1)

    def test_log_contains_only_safe_retry_diagnostics(self):
        c = self.client([response(429, header='2'), response()])
        with self.assertLogs('src.notion_client', level=logging.INFO) as logs:
            c._request('GET', '/pages/private')
        text = '\n'.join(logs.output)
        for field in ['status=429', 'retry=1', 'wait=2.000', 'requests=2']:
            self.assertIn(field, text)
        self.assertNotIn('test-secret', text)
        self.assertNotIn('private', text)

    def test_529_retries_but_blocked_api_does_not(self):
        c = self.client([response(529, header='1'), response()])
        c._request('GET', '/pages/a')
        self.assertEqual(len(self.starts), 2)
        self.setUp()
        c = self.client([response(429, {'additional_data': {'rate_limit_reason': 'public_api_request_blocked'}})])
        with self.assertRaisesRegex(NotionClientError, 'api_access_blocked'):
            c._request('GET', '/pages/a')
        self.assertEqual(self.clock.waits, [])

    def test_invalid_json_and_non_get_abort(self):
        bad = response();bad.json.side_effect = ValueError('PRIVATE-BODY')
        c = self.client([bad])
        with self.assertRaisesRegex(NotionClientError, 'invalid_json'):
            c._request('GET', '/pages/a')
        with self.assertRaisesRegex(NotionClientError, 'unsupported_method'):
            c._request('POST', '/pages/a')

    def test_rate_limit_failure_preserves_local_data_and_never_updates_rag(self):
        with TemporaryDirectory() as directory:
            storage = LocalStorage(Path(directory))
            storage.save_json('processed/reviews.json', [])
            storage.save_json('state/state.json', {})
            storage.save_text('raw/existing.md', 'retained')
            storage.save_text('chroma/sentinel', 'retained index')
            before = {str(p): p.read_bytes() for p in Path(directory).rglob('*') if p.is_file()}
            c = self.client([response(429, header='1'), response(429, header='1')], max_retries=1)
            settings = SimpleNamespace(notion_page_ids=['a'], notion_api_key='test-secret')
            with patch.object(data_loader, 'get_storage', return_value=storage), patch.object(data_loader, 'get_settings', return_value=settings), patch.object(data_loader, 'NotionClient', return_value=c):
                result = data_loader.sync_reviews_from_notion()
            updater = Mock()
            self.assertIsNone(run_rag_update_after_sync(result.debug, [], updater))
            updater.assert_not_called()
            self.assertEqual(before, {str(p): p.read_bytes() for p in Path(directory).rglob('*') if p.is_file()})
            self.assertIn('retry_exhausted', result.debug.messages[-1])
            self.session.close.assert_called_once()

    def test_concurrent_clients_never_send_simultaneously(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        entered, release, second_ready = Event(), Event(), Event()
        active = 0
        peak = 0
        calls = 0
        def request(*args, **kwargs):
            nonlocal active, peak, calls
            active += 1
            peak = max(peak, active)
            calls += 1
            self.starts.append(self.clock())
            if calls == 1:
                entered.set()
                self.assertTrue(release.wait(2))
            active -= 1
            return response()
        self.session.request.side_effect = request
        a = NotionClient('a', limiter=self.limiter, session=self.session)
        b = NotionClient('b', limiter=self.limiter, session=self.session)
        def second():
            second_ready.set()
            return b._request('GET', '/pages/b')
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(a._request, 'GET', '/pages/a')
            try:
                self.assertTrue(entered.wait(2))
                other = pool.submit(second)
                self.assertTrue(second_ready.wait(2))
            finally:
                release.set()
            first.result(timeout=2)
            other.result(timeout=2)
        self.assertEqual(peak, 1)
        self.assertEqual(self.starts, [100, 100.5])

    def test_zero_retry_after_still_obeys_normal_interval(self):
        c = self.client([response(429, header='0'), response()])
        c._request('GET', '/pages/a')
        self.assertEqual(self.starts, [100, 100.5])

    def test_429_after_completed_page_keeps_cached_reviews_and_index(self):
        from src.notion_parser import parse_review
        with TemporaryDirectory() as directory:
            storage = LocalStorage(Path(directory))
            cached = parse_review('# 2026-09-01 Review\n- Date: 2026-09-01\n- Topic: Cached\n- Duration: 10', 'old', '2026-09')
            storage.save_json('processed/reviews.json', [cached.model_dump(mode='json')])
            storage.save_json('state/state.json', {})
            storage.save_text('raw/old.md', 'retained')
            storage.save_text('chroma/sentinel', 'index')
            before = {str(p): p.read_bytes() for p in Path(directory).rglob('*') if p.is_file()}
            c = self.client([response(body={'properties': {'title': {'type': 'title', 'title': [{'plain_text': '2026-10'}]}}}),
                             response(body={'results': []}), response(429, header='1')], max_retries=0)
            settings = SimpleNamespace(notion_page_ids=['first', 'second'], notion_api_key='key')
            with patch.object(data_loader, 'get_storage', return_value=storage), patch.object(data_loader, 'get_settings', return_value=settings), patch.object(data_loader, 'NotionClient', return_value=c):
                result = data_loader.sync_reviews_from_notion()
            self.assertEqual([r.review_id for r in result.reviews], [cached.review_id])
            self.assertEqual(before, {str(p): p.read_bytes() for p in Path(directory).rglob('*') if p.is_file()})
            updater = Mock()
            self.assertIsNone(run_rag_update_after_sync(result.debug, result.reviews, updater))
            updater.assert_not_called()
