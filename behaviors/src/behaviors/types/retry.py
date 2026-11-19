"""Retry schedule configuration."""

import math
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

    Raises:
        ValueError: ``max_attempts`` is not a whole number of at least 1, or one of the other
            numbers is negative, infinite or NaN.
    """

    max_attempts: int = 3
    initial_backoff_seconds: float = 1.0
    backoff_base: float = 3.0
    max_backoff_seconds: float = 120.0
    jitter_seconds: float = 5.0

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise ValueError(f"max_attempts must be a whole number, not {self.max_attempts!r}.")
        if self.max_attempts < 1:
            raise ValueError(f"max_attempts must be at least 1, not {self.max_attempts}.")
        for name in (
            "initial_backoff_seconds",
            "backoff_base",
            "max_backoff_seconds",
            "jitter_seconds",
        ):
            value = getattr(self, name)
            if not (math.isfinite(value) and value >= 0):
                raise ValueError(f"{name} must be a finite number of at least 0, not {value!r}.")

    def backoff_seconds(self, attempt: int) -> float:
        """The wait after the given 1-based attempt number.

        The wait grows by ``backoff_base`` after each attempt until it reaches
        ``max_backoff_seconds``. It stops growing there, so a large attempt number does not
        overflow.
        """
        backoff = self.initial_backoff_seconds
        for _ in range(attempt - 1):
            if backoff >= self.max_backoff_seconds:
                break
            backoff *= self.backoff_base
        return min(backoff, self.max_backoff_seconds) + random.uniform(0, self.jitter_seconds)
