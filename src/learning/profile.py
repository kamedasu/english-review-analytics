"""Self-reported learner profile, separate from task difficulty and inferred ability.

Coach integration is intentionally deferred: changing its serialized profile today
would change existing plan fingerprints. The level/cefr attributes stay compatible.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class LearnerProfile:
    level: str = 'intermediate'
    cefr: str = 'B1–B2'
    level_source: str = 'self_reported'
    goals: tuple[str, ...] = (
        'Natural everyday English', 'Expressive range', 'Practical communication',
    )
    interests: tuple[str, ...] = (
        'Yoga', 'Pilates', 'Hiking', 'Running', 'Cafe / Coffee', 'Travel',
        'International Exchange', 'Socializing', 'Everyday Conversation',
    )


LEARNER_PROFILE = LearnerProfile()
