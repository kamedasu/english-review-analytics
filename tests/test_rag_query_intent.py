import unittest
from datetime import date

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
        self.assertEqual(parse_rag_query("今後使ったほうが良いフレーズ").kind, "phrase_recommendation")
        self.assertEqual(parse_rag_query("最近の弱点").kind, "weakness")
        self.assertEqual(parse_rag_query("成長したところ").kind, "strength")

    def test_parses_explicit_month_formats(self) -> None:
        for query in ("2026年7月", "2026/7", "2026-07", "July 2026"):
            intent = parse_rag_query(query)
            self.assertEqual((intent.start_date, intent.end_date), (date(2026, 7, 1), date(2026, 7, 31)))

    def test_parses_yearless_japanese_and_english_months_from_index_reference_date(self) -> None:
        latest = date(2026, 8, 15)
        for query in ("7月", "7月の中で", "7月中", "in July"):
            intent = parse_rag_query(query, latest)
            self.assertEqual((intent.start_date, intent.end_date), (date(2026, 7, 1), date(2026, 7, 31)))
        december = parse_rag_query("12月", latest)
        self.assertEqual((december.start_date, december.end_date), (date(2025, 12, 1), date(2025, 12, 31)))

    def test_parses_relative_months_from_index_reference_date(self) -> None:
        latest = date(2026, 8, 15)
        for query in ("今月", "this month"):
            intent = parse_rag_query(query, latest)
            self.assertEqual((intent.start_date, intent.end_date), (date(2026, 8, 1), date(2026, 8, 31)))
        for query in ("先月", "last month"):
            intent = parse_rag_query(query, latest)
            self.assertEqual((intent.start_date, intent.end_date), (date(2026, 7, 1), date(2026, 7, 31)))

    def test_explicit_month_takes_priority_over_recent(self) -> None:
        intent = parse_rag_query("最近の7月の表現", date(2026, 8, 15))
        self.assertTrue(intent.recent)
        self.assertEqual((intent.start_date, intent.end_date), (date(2026, 7, 1), date(2026, 7, 31)))
