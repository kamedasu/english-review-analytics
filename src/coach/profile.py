"""Conservative practice suitability heuristics, not an automatic CEFR assessment."""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class LearnerProfile:
    level: str = 'intermediate'
    cefr: str = 'B1–B2'


LEARNER_PROFILE = LearnerProfile()
LOGIC_VERSION = '3'

# These examples supplement structural and history-based assessment. Even these
# targets remain eligible when focused correction/confusion evidence exists.
FOUNDATION_PHRASES = {
    'in front of', 'go back home', 'go home', 'hang out', 'in / out', 'in/out',
    'i just woke up', 'on my way', 'i usually',
    'at home', 'get up', 'go to school', 'come back', 'a lot of',
}
FOUNDATION_WORDS = {
    'player', 'person', 'people', 'friend', 'home', 'school', 'work', 'place',
    'go', 'come', 'get', 'do', 'make', 'have', 'eat', 'drink', 'run', 'walk',
    'in', 'out', 'on', 'at', 'to', 'of', 'a', 'the', 'i', 'it', 'you', 'is', 'not',
    'very', 'good', 'bad', 'nice', 'big', 'small', 'happy', 'sad', 'want', 'like',
    'play', 'see', 'watch', 'buy', 'can', 'my', 'your', 'back', 'front', 'cafe', 'coffee',
}
# A few transparent practice families; members are never invented if absent in history.
PRACTICE_FAMILIES = (
    frozenset({'refresh', 'refreshed', 'refreshing'}),
    frozenset({'overdo it', 'overdid it'}),
    frozenset({'unwind', 'wind down'}),
)
NUANCE_TERMS = re.compile(
    r'使い分け|混同|間違|誤用|誤り|迷[いうっ]|活用|時制|品詞|前置詞|冠詞|語順|過去形|過去分詞|'
    r'ニュアンス|違い|confus|mistak|incorrect|tense|adjective|adverb|preposition|nuance|\bvs\.?\b', re.I)
GRAMMAR_TERMS = ('時制', '過去形', '現在完了', '冠詞', '前置詞', '複数形', '語順', '品詞',
                 '条件文', '仮定法', '受動態', 'tense', 'preposition', 'article')


def word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z]+(?:['’\-][A-Za-z]+)*", text))


def is_foundation(key: str) -> bool:
    words = re.findall(r"[a-z]+(?:['-][a-z]+)*", key)
    routine = (bool(re.match(
        r"^(?:(?:i|we|you|they|he|she) (?:usually|often|always|sometimes)\b|"
        r"(?:i|we|you|they) just (?:woke up|got up|came home|got home)\b|"
        r"on (?:my|your|his|her|our|their) way\b)", key)))
    return (routine or key in FOUNDATION_PHRASES or key in FOUNDATION_WORDS
            or bool(words) and all(word in FOUNDATION_WORDS for word in words))


def suitability(key: str, fields: set[str], focused: bool) -> str:
    if is_foundation(key):
        return 'foundation_with_evidence' if focused else 'insufficient'
    if focused:
        return 'history_based_challenge'
    if any(key in family for family in PRACTICE_FAMILIES):
        return 'intermediate_application'
    # Unknown single words are not promoted just because they look difficult.
    # Multiword targets must come from explicit learning fields, not advice prose.
    if word_count(key) >= 2 and fields - {'weak_points'}:
        return 'intermediate_application'
    return 'insufficient'


def application_value(key: str) -> int:
    if is_foundation(key):
        return 0
    if any(key in family for family in PRACTICE_FAMILIES):
        return 2
    words = word_count(key)
    return 2 if 2 <= words <= 8 else 1


DEFAULT_PRACTICE_LEVEL = 'applied'
PRACTICE_LEVELS = {
    'basic': {
        'label': '基本（Basic）',
        'instruction': 'Practice one intermediate expression accurately in a short natural response; focus on the required grammar. Basic changes task complexity, not vocabulary to beginner level.',
    },
    'applied': {
        'label': '応用（Applied）',
        'instruction': 'Combine the target with another supplied expression or a useful grammar pattern. Require a choice of tense, word form, preposition or nuance in a concrete situation. Complexity comes from communicative choices, never added words.',
    },
    'real_conversation': {
        'label': '実践（Real Conversation）',
        'instruction': 'Imagine a real interaction: give a feeling or reason and continue naturally with a question or polite invitation, considering the other person response. Use natural conversational turns, not a list of unrelated sentences.',
    },
}
CATEGORY_LABELS = {'weakness': '弱点克服', 'expression': '表現力アップ', 'conversation': '実践ミッション', 'legacy': '練習'}
