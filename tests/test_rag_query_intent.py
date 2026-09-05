import unittest

from src.rag.query_intent import DEFAULT_RESULT_COUNT, MAX_REQUESTED_RESULTS, parse_rag_query


class RagQueryIntentTest(unittest.TestCase):
    def test_extracts_japanese_and_english_requested_counts(self) -> None:
        self.assertEqual(parse_rag_query("最近の表現を10個教えて").requested_count, 10)
        self.assertEqual(parse_rag_query("20個頂戴").requested_count, 20)
        self.assertEqual(parse_rag_query("give me 15 phrases").requested_count, 15)

    def test_uses_default_and_caps_requested_count(self) -> None:
        self.assertEqual(parse_rag_query("最近の表現を教えて").requested_count, DEFAULT_RESULT_COUNT)
        self.assertEqual(parse_rag_query("100個教えて").requested_count, MAX_REQUESTED_RESULTS)

    def test_detects_recent_queries(self) -> None:
        self.assertTrue(parse_rag_query("最近の表現を教えて").recent)
        self.assertTrue(parse_rag_query("recent expressions").recent)

    def test_classifies_supported_intents(self) -> None:
        self.assertEqual(parse_rag_query("自然なフレーズを教えて").kind, "natural_expression")
        self.assertEqual(parse_rag_query("直された表現を教えて").kind, "natural_expression")
        self.assertEqual(parse_rag_query("ネイティブが特に使うフレーズ").kind, "phrase_recommendation")
        self.assertEqual(parse_rag_query("覚えたほうがいい言い回し").kind, "phrase_recommendation")
        self.assertEqual(parse_rag_query("最近の弱点").kind, "weakness")
        self.assertEqual(parse_rag_query("成長したところ").kind, "strength")
