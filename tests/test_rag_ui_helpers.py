import unittest

from src.rag.answerer import RagAnswerGenerationError
from src.rag.chroma_store import RagIndexNotFoundError
from src.rag.embeddings import EmbeddingError
from src.rag.models import RagDocument
from src.rag.retriever import RetrievedDocument
from src.rag.ui_helpers import (
    prepare_rag_answer_request,
    rag_error_message,
    should_run_rag_answer,
    source_display,
)


class RagUiHelpersTest(unittest.TestCase):
    def test_only_submitted_non_blank_queries_should_run(self) -> None:
        self.assertFalse(should_run_rag_answer(False, "question"))
        self.assertFalse(should_run_rag_answer(True, "  "))
        self.assertTrue(should_run_rag_answer(True, "question"))

    def test_submissions_use_the_current_input_and_keep_it_separate_from_last_query(self) -> None:
        state: dict[str, object] = {"rag_input_query": "Question A"}

        self.assertEqual(prepare_rag_answer_request(True, "Question A", state), "Question A")
        self.assertEqual(state["rag_last_query"], "Question A")
        self.assertEqual(state["rag_input_query"], "Question A")

        state["rag_input_query"] = "Question B"
        self.assertEqual(prepare_rag_answer_request(True, "Question B", state), "Question B")
        self.assertEqual(state["rag_last_query"], "Question B")
        self.assertEqual(state["rag_input_query"], "Question B")

        state["rag_input_query"] = "Question C"
        self.assertEqual(prepare_rag_answer_request(True, "Question C", state), "Question C")
        self.assertEqual(state["rag_last_query"], "Question C")
        self.assertEqual(state["rag_input_query"], "Question C")

    def test_rerun_and_blank_submission_do_not_create_an_answer_request(self) -> None:
        state: dict[str, object] = {"rag_last_query": "Previous question", "rag_answer": "previous"}
        self.assertIsNone(prepare_rag_answer_request(False, "New text", state))
        self.assertEqual(state["rag_last_query"], "Previous question")
        self.assertIsNone(prepare_rag_answer_request(True, "   ", state))
        self.assertEqual(state["rag_error"], "質問を入力してください。")

    def test_source_display_preserves_ordered_source_data_and_uses_readable_type(self) -> None:
        source = RetrievedDocument(
            RagDocument("review:1", "Weak point: prepositions", {
                "type": "weak_point", "date": "2026-07-09", "topic": "grammar",
            }),
            0.12,
        )
        display = source_display(source, 3)
        self.assertEqual(display.number, 3)
        self.assertEqual(display.type_label, "Weak Point")
        self.assertEqual(display.date, "2026-07-09")
        self.assertEqual(display.topic, "grammar")
        self.assertEqual(display.text, "Weak point: prepositions")
        self.assertFalse(hasattr(display, "document_id"))
        self.assertFalse(hasattr(display, "distance"))

    def test_rag_errors_are_mapped_to_user_friendly_messages(self) -> None:
        self.assertIn("python -m src.rag.indexer", rag_error_message(RagIndexNotFoundError("missing")))
        self.assertIn("OPENAI_API_KEY", rag_error_message(ValueError("OPENAI_API_KEY is required")))
        self.assertIn("検索処理", rag_error_message(EmbeddingError("failed")))
        self.assertIn("回答の生成", rag_error_message(RagAnswerGenerationError("failed")))
