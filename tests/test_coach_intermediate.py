from datetime import date
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from src.models import Review, PhraseCard, MoreNaturalExpression
from src.coach.candidates import extract_candidates, normalize, evidence, data_version
from src.coach.profile import LEARNER_PROFILE, LOGIC_VERSION, word_count
from src.coach.service import (CoachError, OpenAICoach, PROMPT_VERSION, candidate_input,
                               generate_plan, is_stale, load_plan, PLAN_PATH)
from src.storage.local_storage import LocalStorage
from tests.test_learning_coach import Provider, fixture, payload, legacy_draft


def review(phrases, *, weak=(), corrections=(), review_id='r', day=1):
    return Review(review_id=review_id, date=date(2026, 10, day), topic='Outdoor yoga',
                  phrase_cards=[PhraseCard(phrase=p) for p in phrases], weak_points=list(weak),
                  more_natural_expressions=list(corrections))


class IntermediateSelectionTests(unittest.TestCase):
    def test_foundation_words_are_not_normal_candidates_or_fillers(self):
        basics = ['player', 'in front of', 'go back home', 'hang out', 'in / out', 'home', 'run', 'very good', 'at the cafe', 'I want to go home']
        self.assertEqual(extract_candidates([review(basics)]), [])
        selected = extract_candidates([review(basics + ['make the most of'])])
        self.assertEqual([c.expression for c in selected], ['make the most of'])

    def test_unknown_difficult_single_word_is_not_automatically_better(self):
        self.assertEqual(extract_candidates([review(['sesquipedalian'])]), [])

    def test_weak_mentions_alone_do_not_raise_basic_words(self):
        rows = [review(['player'], weak=['player'], review_id=str(i), day=i) for i in [1, 2, 3]]
        self.assertEqual(extract_candidates(rows), [])

    def test_explicit_basic_usage_problem_is_eligible(self):
        row = review(['player'], weak=['「player」の使い分けで混同がある'])
        selected = extract_candidates([row])
        self.assertEqual(selected[0].expression, 'player')
        self.assertEqual(selected[0].level_fit, 'foundation_with_evidence')
        self.assertEqual(selected[0].nuance_review_count, 1)
        self.assertEqual(selected[0].practice_evidence[0].source.review_id, 'r')

    def test_basic_word_in_unchanged_context_is_not_a_correction_target(self):
        row = review(['player'], corrections=[MoreNaturalExpression(
            your_phrase='The player go home.', more_natural='The player went home.', note='時制を修正')])
        self.assertNotIn('player', [c.expression for c in extract_candidates([row])])

    def test_targeted_correction_ranks_before_age_only(self):
        old = review(['make the most of'], review_id='old', day=1)
        new = review(['take it easy'], review_id='new', day=30, corrections=[MoreNaturalExpression(
            your_phrase='take it easily', more_natural='take it easy', note='easyとeasilyの品詞の使い分け')])
        selected = extract_candidates([old, new])
        target = next(c for c in selected if c.expression == 'take it easy')
        self.assertEqual(selected[0].candidate_id, target.candidate_id)
        self.assertEqual(target.correction_review_count, 1)
        self.assertEqual(target.nuance_review_count, 1)

    def test_refresh_forms_are_grounded_and_grouped_not_invented(self):
        row = review(['refresh', 'refreshed', 'refreshing'],
                     weak=['refresh / refreshed / refreshing の品詞の使い分けで混同'],
                     corrections=[MoreNaturalExpression(your_phrase='I refresh after yoga',
                         more_natural='I felt refreshed after yoga', note='refreshの活用とrefreshingの使い分け')])
        selected = extract_candidates([row])
        family = next(c for c in selected if normalize(c.expression) in {'refresh', 'refreshed', 'refreshing'})
        forms = {normalize(family.expression), *(normalize(t.expression) for t in family.related_targets)}
        self.assertEqual(forms, {'refresh', 'refreshed', 'refreshing'})
        self.assertEqual(sum(normalize(c.expression) in forms for c in selected), 1)
        self.assertNotIn('refreshment', forms)
        self.assertTrue(family.practice_evidence)
        for source in family.sources:
            self.assertEqual((source.review_id, source.date, source.topic), ('r', row.date, row.topic))

    def test_slash_contrast_in_note_can_extract_recorded_forms(self):
        row = review([], corrections=[MoreNaturalExpression(note='refresh / refreshed / refreshing の品詞の使い分け')])
        selected = extract_candidates([row])
        forms = {normalize(c.expression) for c in selected}
        forms.update(normalize(t.expression) for c in selected for t in c.related_targets)
        self.assertEqual(forms, {'refresh', 'refreshed', 'refreshing'})

    def test_family_does_not_add_unrecorded_variants(self):
        selected = extract_candidates([review(['overdo it'])])
        self.assertEqual([c.expression for c in selected], ['overdo it'])
        self.assertEqual(selected[0].related_targets, [])

    def test_case_punctuation_variants_are_one_candidate(self):
        row = review(['Unwind', ' “UNWIND.” ', 'wind down'])
        selected = extract_candidates([row])
        self.assertEqual(len(selected), 1)
        self.assertEqual(len(selected[0].related_targets), 1)
        self.assertEqual(selected[0].occurrence_count, 1)

    def test_repeated_japanese_grammar_points_link_to_real_corrections(self):
        rows = [review([], review_id=str(i), day=i, weak=['過去形を練習する'], corrections=[MoreNaturalExpression(
            your_phrase='I go hiking yesterday', more_natural='I went hiking yesterday', note='過去形の時制')]) for i in [1, 2]]
        selected = extract_candidates(rows)
        target = next(c for c in selected if c.expression == 'I went hiking yesterday')
        self.assertEqual(target.weak_review_count, 2)
        self.assertEqual(target.correction_review_count, 2)
        self.assertEqual(target.nuance_review_count, 2)
        self.assertIn('repeated_weak_point', target.selection_factors)

    def test_note_reappearance_updates_last_seen_without_claiming_actual_use(self):
        old = review(['unwind'], review_id='old', day=1)
        recent = review([], review_id='recent', day=30,
                        corrections=[MoreNaturalExpression(note='unwindの使い分けを確認')])
        c = extract_candidates([old, recent])[0]
        self.assertEqual(c.last_seen, recent.date)
        self.assertEqual(c.days_since_seen, 0)
        self.assertEqual(c.occurrence_count, 2)
        self.assertIsNone(c.last_recorded_use)

    def test_short_applied_phrase_precedes_single_word_with_similar_evidence(self):
        row = review(['join', 'make the most of'], weak=['「join」の使い分け', '「make the most of」の使い分け'])
        selected = extract_candidates([row])
        self.assertEqual(selected[0].expression, 'make the most of')

    def test_identical_meaning_deduplicates_without_losing_sources(self):
        a = review(['make the most of'], review_id='a')
        a.phrase_cards[0].meaning = '最大限活用する'
        b = review(['get the most out of'], review_id='b', day=2)
        b.phrase_cards[0].meaning = '最大限活用する'
        selected = extract_candidates([a, b])
        self.assertEqual(len(selected), 1)
        self.assertEqual({s.review_id for s in selected[0].sources} | {s.review_id for t in selected[0].related_targets for s in t.sources}, {'a', 'b'})

    def test_evidence_facts_are_counts_and_not_mastery_judgments(self):
        row = review(['unwind'], weak=['「unwind」の使い分け'])
        c = extract_candidates([row])[0]
        text = evidence(c)
        self.assertIn('使い分け等の指摘 1件', text)
        self.assertIn('判定ではありません', text)
        self.assertNotIn('完全に忘れて', text)
        self.assertEqual(candidate_input(c)['nuance_review_count'], 1)
        self.assertIn('practice_evidence', candidate_input(c))


class IntermediateGenerationTests(unittest.TestCase):
    def test_profile_and_prompt_require_situational_intermediate_practice(self):
        cs = extract_candidates(fixture())
        response = Mock(status_code=200)
        response.json.return_value = {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(payload(cs))}]}]}
        with patch('src.coach.service.requests.post', return_value=response) as post:
            OpenAICoach('fake', 'mock').generate(cs)
        sent = post.call_args.kwargs['json']
        inp = json.loads(sent['input'])
        self.assertEqual(inp['learner_profile']['level'], 'intermediate')
        self.assertEqual(inp['learner_profile']['cefr'], LEARNER_PROFILE.cefr)
        self.assertIn('no mandatory word count', sent['instructions'])
        self.assertIn('concrete Japanese', sent['instructions'])
        self.assertIn('tense/prepositions/word forms', sent['instructions'])
        self.assertTrue(sent['text']['format']['strict'])

    def test_new_examples_have_profile_and_version(self):
        with TemporaryDirectory() as d:
            plan = generate_plan(fixture(), LocalStorage(Path(d)), provider=Provider())
            self.assertEqual(plan.logic_version, LOGIC_VERSION)
            self.assertEqual(plan.prompt_version, PROMPT_VERSION)
            self.assertEqual(plan.learner_level, 'intermediate')
            for lesson in plan.lessons:
                self.assertGreater(word_count(lesson.generated.example_en), 0)

    def test_concise_examples_are_not_rejected_for_word_count(self):
        with TemporaryDirectory() as d:
            storage = LocalStorage(Path(d))
            previous = generate_plan(fixture(), storage, provider=Provider())
            bad = payload(extract_candidates(fixture()))
            bad['items'][0]['example_en'] = 'Let us unwind.'
            provider = Mock(model='mock')
            provider.generate.return_value = (bad, previous.usage)
            current = generate_plan(fixture(), storage, provider=provider, regenerate=True)
            self.assertNotEqual(current.plan_id, previous.plan_id)
            self.assertEqual(current.lessons[0].generated.example_en, 'Let us unwind.')

    def test_unqualified_basics_do_not_call_provider_or_save(self):
        with TemporaryDirectory() as d:
            storage = LocalStorage(Path(d));provider = Mock()
            with self.assertRaises(CoachError):
                generate_plan([review(['player', 'go back home'])], storage, provider=provider)
            provider.generate.assert_not_called()
            self.assertFalse(storage.exists(PLAN_PATH))

    def test_legacy_cache_loads_and_never_regenerates_automatically(self):
        with TemporaryDirectory() as d:
            storage = LocalStorage(Path(d))
            current = generate_plan(fixture(), storage, provider=Provider())
            old = current.model_dump(mode='json')
            old.pop('logic_version');old.pop('learner_level');old.pop('practice_level');old['prompt_version'] = '1';old['schema_version'] = 1
            for lesson in old['lessons']:
                for key in ['correction_review_count', 'nuance_review_count', 'level_fit', 'application_value', 'practice_evidence', 'related_targets', 'selection_factors']:
                    lesson['candidate'].pop(key)
                lesson['generated'] = legacy_draft(lesson['candidate']['candidate_id'])
                lesson.pop('category');lesson.pop('support_origin')
            storage.save_json(PLAN_PATH, old)
            before = storage.load_text(PLAN_PATH)
            legacy = load_plan(storage)
            self.assertTrue(is_stale(legacy, fixture()))
            provider = Mock()
            self.assertEqual(generate_plan(fixture(), storage, provider=provider), legacy)
            provider.generate.assert_not_called()
            self.assertEqual(storage.load_text(PLAN_PATH), before)
            fresh = generate_plan(fixture(), storage, provider=Provider(), regenerate=True)
            self.assertFalse(is_stale(fresh, fixture()))
            self.assertEqual(fresh.generation_number, 2)

    def test_prompt_only_change_marks_cache_stale(self):
        with TemporaryDirectory() as d:
            plan = generate_plan(fixture(), LocalStorage(Path(d)), provider=Provider())
            with patch('src.coach.service.PROMPT_VERSION', 'future'):
                self.assertTrue(is_stale(plan, fixture()))
            self.assertFalse(is_stale(plan, fixture()))

    def test_profile_change_invalidates_data_version(self):
        first = data_version(fixture())
        from src.coach.profile import LearnerProfile
        with patch('src.coach.candidates.LEARNER_PROFILE', LearnerProfile(level='advanced')):
            self.assertNotEqual(first, data_version(fixture()))
