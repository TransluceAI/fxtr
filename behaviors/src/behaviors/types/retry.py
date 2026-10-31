"""Retry schedule configuration."""

import random
from dataclasses import dataclass

from behaviors.types.namespace import behaviors_type
from fxtr.entity_defns.dataclass_bases import TaggedDataclass


@dataclass(frozen=True)
class RetryConfig(TaggedDataclass, fxtr_type=behaviors_type("RetryConfig.v1")):
    """How many times to retry and how to back off.

    Only the schedule: *what* to retry (failure categories, exception types)
    is not configurable here and belongs to whatever applies the config, e.g.
    ``behaviors.implementations.models.from_api``'s
    ``failure_categories_to_retry`` argument.

    Attributes:
        max_attempts: Total attempts including the first.
        initial_backoff_seconds: Wait before the first retry.
        backoff_base: Multiplier applied to the wait after each attempt.
        max_backoff_seconds: Cap on the wait (before jitter).
        jitter_seconds: A uniform random addition of up to this many seconds
            on every wait.
    """

    max_attempts: int = 3
    initial_backoff_seconds: float = 1.0
    backoff_base: float = 3.0
    max_backoff_seconds: float = 120.0
    jitter_seconds: float = 5.0

    def backoff_seconds(self, attempt: int) -> float:
        """The wait after the given 1-based attempt number."""
        backoff = min(
            self.initial_backoff_seconds * self.backoff_base ** (attempt - 1),
            self.max_backoff_seconds,
        )
        return backoff + random.uniform(0, self.jitter_seconds)
