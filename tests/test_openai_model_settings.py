from contextlib import contextmanager
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import requests

from src.config import get_settings
from src.analytics import summarize_reviews
from src.coach.service import CoachError, generate_plan, load_plan
from src.coach.candidates import extract_candidates
from src.llm_summary import generate_period_summary
from src.rag.answerer import OpenAIAnswerGenerationProvider, RagAnswerGenerationError
from src.rag.embeddings import OpenAIEmbeddingProvider, EmbeddingError
from src.rag.indexer import _require_compatible_collection
from src.rag.chroma_store import RagIndexRebuildRequiredError
from src.storage.local_storage import LocalStorage
from tests.test_learning_coach import fixture, payload, Provider

NAMES = ('OPENAI_MODEL', 'OPENAI_RAG_MODEL', 'OPENAI_EMBEDDING_MODEL')


@contextmanager
def settings_env(**values):
    with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-key'}):
        for name in NAMES:
            os.environ.pop(name, None)
        os.environ.update(values)
        yield


def reply(status=200, body=None):
    r = Mock(status_code=status, text='PRIVATE API BODY test-key')
    r.json.return_value = body or {'output_text': 'Generated text'}
    return r


class ModelSettingsTests(unittest.TestCase):
    def test_unset_models_keep_compatibility_defaults(self):
        with settings_env():
            s = get_settings()
        self.assertEqual((s.openai_model, s.openai_rag_model, s.openai_embedding_model),
                         ('gpt-4.1-mini', 'gpt-4.1-mini', 'text-embedding-3-small'))

    def test_blank_or_whitespace_use_defaults_and_values_are_trimmed(self):
        for empty in ['', '  ']:
            with settings_env(**{k: empty for k in NAMES}):
                s = get_settings()
                self.assertEqual(s.openai_model, 'gpt-4.1-mini')
                self.assertEqual(s.openai_rag_model, 'gpt-4.1-mini')
                self.assertEqual(s.openai_embedding_model, 'text-embedding-3-small')
        with settings_env(OPENAI_MODEL=' gpt-6.1-terra ', OPENAI_RAG_MODEL=' custom-rag ', OPENAI_EMBEDDING_MODEL=' text-embedding-3-small '):
            s = get_settings()
            self.assertEqual((s.openai_model, s.openai_rag_model, s.openai_embedding_model),
                             ('gpt-6.1-terra', 'custom-rag', 'text-embedding-3-small'))

    def test_coach_uses_configured_model_and_keeps_structured_outputs(self):
        candidates = extract_candidates(fixture())
        response = reply(body={'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': __import__('json').dumps(payload(candidates))}]}]})
        with TemporaryDirectory() as directory, settings_env(OPENAI_MODEL='gpt-6.1-terra'), patch('requests.post', return_value=response) as post:
            plan = generate_plan(fixture(), LocalStorage(Path(directory)))
        request = post.call_args.kwargs['json']
        self.assertEqual(request['model'], 'gpt-6.1-terra')
        self.assertEqual(request['text']['format']['type'], 'json_schema')
        self.assertTrue(request['text']['format']['strict'])
        self.assertEqual(len(plan.missions), 3)
        self.assertEqual(plan.model, 'gpt-6.1-terra')

    def test_analytics_uses_common_model_and_rag_remains_independent(self):
        reviews = fixture()
        with settings_env(OPENAI_MODEL='gpt-6.1-terra'), patch('requests.post', return_value=reply()) as post:
            result = generate_period_summary(summarize_reviews(reviews, 'all'), reviews)
            self.assertEqual(result.source, 'llm')
            self.assertEqual(post.call_args.kwargs['json']['model'], 'gpt-6.1-terra')
            rag = OpenAIAnswerGenerationProvider.from_settings()
            rag.generate_answer('question', 'context')
            self.assertEqual(post.call_args.kwargs['json']['model'], 'gpt-4.1-mini')

    def test_rag_override_does_not_change_common_model(self):
        with settings_env(OPENAI_MODEL='gpt-6.1-terra', OPENAI_RAG_MODEL='rag-custom'), patch('requests.post', return_value=reply()) as post:
            OpenAIAnswerGenerationProvider.from_settings().generate_answer('question', 'context')
            self.assertEqual(post.call_args.kwargs['json']['model'], 'rag-custom')
            self.assertEqual(get_settings().openai_model, 'gpt-6.1-terra')

    def test_embeddings_keep_model_dimension_and_compatibility_checks(self):
        vector = [0.0] * 1536
        with settings_env(OPENAI_MODEL='gpt-6.1-terra', OPENAI_RAG_MODEL='rag-custom'), patch('requests.post', return_value=reply(body={'data': [{'index': 0, 'embedding': vector}]})) as post:
            provider = OpenAIEmbeddingProvider.from_settings()
            self.assertEqual(len(provider.embed_texts(['text'])[0]), 1536)
            self.assertEqual(post.call_args.kwargs['json']['model'], 'text-embedding-3-small')
            self.assertNotIn('dimensions', post.call_args.kwargs['json'])
            _require_compatible_collection({'embedding_model': 'text-embedding-3-small'}, get_settings().openai_embedding_model)
            with self.assertRaises(RagIndexRebuildRequiredError):
                _require_compatible_collection({'embedding_model': 'other'}, get_settings().openai_embedding_model)

    def test_default_provider_constructors_use_same_constants(self):
        with settings_env():
            s = get_settings()
            self.assertEqual(OpenAIAnswerGenerationProvider('key')._model, s.openai_rag_model)
            self.assertEqual(OpenAIEmbeddingProvider('key')._model, s.openai_embedding_model)

    def test_invalid_model_api_error_preserves_coach_and_original_files(self):
        with TemporaryDirectory() as directory:
            storage = LocalStorage(Path(directory))
            previous = generate_plan(fixture(), storage, provider=Provider())
            storage.save_text('processed/sentinel', 'retained')
            before = {str(p): p.read_bytes() for p in Path(directory).rglob('*') if p.is_file()}
            with settings_env(OPENAI_MODEL='invalid-model'), patch('requests.post', return_value=reply(404)), self.assertRaises(CoachError) as error:
                generate_plan(fixture(), storage, regenerate=True)
            self.assertEqual(load_plan(storage), previous)
            self.assertNotIn('test-key', str(error.exception))
            self.assertEqual(before, {str(p): p.read_bytes() for p in Path(directory).rglob('*') if p.is_file()})

    def test_model_errors_in_analytics_and_rag_are_sanitized(self):
        with settings_env(OPENAI_MODEL='invalid-model', OPENAI_RAG_MODEL='invalid-model'), patch('requests.post', return_value=reply(404)):
            result = generate_period_summary(summarize_reviews(fixture(), 'all'), fixture())
            self.assertEqual(result.source, 'rule-based')
            self.assertNotIn('PRIVATE', result.warning)
            self.assertNotIn('test-key', result.warning)
            with self.assertRaises(RagAnswerGenerationError) as error:
                OpenAIAnswerGenerationProvider.from_settings().generate_answer('q', 'context')
            self.assertNotIn('PRIVATE', str(error.exception))
            self.assertNotIn('test-key', str(error.exception))

    def test_embedding_errors_are_sanitized(self):
        with settings_env(OPENAI_EMBEDDING_MODEL='invalid-model'), patch('requests.post', return_value=reply(404)), self.assertRaises(EmbeddingError) as error:
            OpenAIEmbeddingProvider.from_settings().embed_texts(['private text'])
        self.assertNotIn('PRIVATE', str(error.exception))
        self.assertNotIn('test-key', str(error.exception))

    def test_transport_error_details_are_not_displayed(self):
        with settings_env(), patch('requests.post', side_effect=requests.Timeout('test-key PRIVATE')):
            result = generate_period_summary(summarize_reviews(fixture(), 'all'), fixture())
            self.assertNotIn('test-key', result.warning)
            with self.assertRaises(RagAnswerGenerationError) as error:
                OpenAIAnswerGenerationProvider.from_settings().generate_answer('q', 'context')
            self.assertNotIn('test-key', str(error.exception))
