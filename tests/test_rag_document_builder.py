from datetime import date, datetime
import unittest

from src.models import MoreNaturalExpression, PhraseCard, Review
from src.rag.document_builder import build_rag_documents


def _review() -> Review:
    return Review(
        review_id="review-2026-01-15",
        source_page_id="notion-page-1",
        review_type="conversation",
        date=date(2026, 1, 15),
        topic="weekend plans",
        good_points=["I explained my weekend smoothly.", "I asked a follow-up question."],
        weak_points=["I confused in and on.", "I spoke too quickly."],
        more_natural_expressions=[
            MoreNaturalExpression(
                your_phrase="I went shopping yesterday.",
                more_natural="I did some shopping yesterday.",
                note="Use did some shopping for the activity.",
            ),
            MoreNaturalExpression(more_natural="That sounds great."),
        ],
        phrase_cards=[
            PhraseCard(
                phrase="catch up",
                meaning="talk after time apart",
                example="Let's catch up next week.",
                source="review",
                priority="high",
                review_status="learning",
                next_review_date=date(2026, 1, 22),
                source_review_date=date(2026, 1, 15),
            ),
            PhraseCard(phrase="sounds good", meaning="I agree", example="Friday sounds good."),
        ],
        raw_markdown="# Entire raw review that must never become a document",
    )


class RagDocumentBuilderTest(unittest.TestCase):
    def test_builds_semantic_documents_with_chroma_safe_metadata(self) -> None:
        review = _review()
        documents = build_rag_documents([review])

        self.assertEqual(len(documents), 8)
        self.assertEqual([document.metadata["type"] for document in documents], [
            "good_point", "good_point", "weak_point", "weak_point",
            "more_natural_expression", "more_natural_expression", "phrase_card", "phrase_card",
        ])
        self.assertEqual(documents[0].id, "review-2026-01-15:good_point:0")
        self.assertEqual(documents[0].text, "Good point: I explained my weekend smoothly.")
        self.assertEqual(documents[2].text, "Weak point: I confused in and on.")
        self.assertEqual(documents[4].text, (
            "Your phrase: I went shopping yesterday.\n"
            "More natural: I did some shopping yesterday.\n"
            "Note: Use did some shopping for the activity."
        ))
        self.assertEqual(documents[6].text, (
            "Phrase: catch up\nMeaning: talk after time apart\nExample: Let's catch up next week."
        ))
        self.assertNotIn("# Entire raw review", {document.text for document in documents})

        common = documents[0].metadata
        self.assertEqual(common, {
            "type": "good_point", "review_id": "review-2026-01-15", "date": "2026-01-15",
            "year": 2026, "month": "2026-01", "topic": "weekend plans",
            "review_type": "conversation", "item_index": 0, "source_page_id": "notion-page-1",
        })
        phrase_metadata = documents[6].metadata
        self.assertEqual(phrase_metadata["source"], "review")
        self.assertEqual(phrase_metadata["priority"], "high")
        self.assertEqual(phrase_metadata["review_status"], "learning")
        self.assertEqual(phrase_metadata["next_review_date"], "2026-01-22")
        self.assertEqual(phrase_metadata["source_review_date"], "2026-01-15")
        for document in documents:
            self.assertTrue(all(isinstance(value, (str, int, float, bool)) for value in document.metadata.values()))
            self.assertFalse(any(isinstance(value, (date, datetime)) for value in document.metadata.values()))


    def test_skips_blank_items_and_ids_are_deterministic_and_unique(self) -> None:
        review = _review().model_copy(
            update={
                "good_points": ["  "],
                "weak_points": ["", "\t"],
                "more_natural_expressions": [MoreNaturalExpression(note=" ")],
                "phrase_cards": [PhraseCard(phrase=" ")],
            }
        )

        self.assertEqual(build_rag_documents([review]), [])
        first = build_rag_documents([_review()])
        second = build_rag_documents([_review()])
        self.assertEqual([document.id for document in first], [document.id for document in second])
        self.assertEqual(len({document.id for document in first}), len(first))

    def test_omits_empty_optional_text_labels(self) -> None:
        review = _review().model_copy(
            update={
                "good_points": [], "weak_points": [], "phrase_cards": [],
                "more_natural_expressions": [MoreNaturalExpression(your_phrase="A phrase")],
            }
        )
        document = build_rag_documents([review])[0]
        self.assertEqual(document.text, "Your phrase: A phrase")

