"""Entity types and data generation for the server demo."""

from __future__ import annotations

import dataclasses
import random
import uuid

from fxtr2.conversion.converters.mashumaro_entities import Child, DataclassEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_ref_scope import EntityRef

# ---------------------------------------------------------------------------
# Type definitions
# ---------------------------------------------------------------------------

_DEMO_NS = uuid.UUID("f1a2b3c4-d5e6-7890-abcd-ef1234567890")


@dataclasses.dataclass(frozen=True)
class ReviewComment(
    DataclassEntity,
    fxtr_type=system_entities.define_type(_DEMO_NS, "review_comment"),
):
    """A single review comment on a code change."""

    author: str
    body: str
    severity: str  # "nit", "suggestion", "blocking"


@dataclasses.dataclass(frozen=True)
class CodeReview(
    DataclassEntity,
    fxtr_type=system_entities.define_type(_DEMO_NS, "code_review"),
):
    """A code review containing multiple comments."""

    file_path: str
    comments: list[Child[ReviewComment]]


@dataclasses.dataclass(frozen=True)
class ReviewerConfig(
    DataclassEntity,
    fxtr_type=system_entities.define_type(_DEMO_NS, "reviewer_config"),
):
    """Configuration for an automated code reviewer."""

    focus_area: str
    model_name: str


@dataclasses.dataclass(frozen=True)
class ReviewSummary(
    DataclassEntity,
    fxtr_type=system_entities.define_type(_DEMO_NS, "review_summary"),
):
    """A summary produced by evaluating a code review with a reviewer config."""

    review: EntityRef[CodeReview]
    reviewer_config: EntityRef[ReviewerConfig]
    overall_score: float
    summary_text: str


# ---------------------------------------------------------------------------
# Hardcoded reviewer configs
# ---------------------------------------------------------------------------

REVIEWER_CONFIGS: dict[str, ReviewerConfig] = {
    "correctness": ReviewerConfig(
        focus_area="focus: Check for logical errors, off-by-one bugs, and incorrect assumptions.",
        model_name="claude-sonnet-4-20250514",
    ),
    "style": ReviewerConfig(
        focus_area="focus: Check for coding style, naming conventions, and readability.",
        model_name="claude-haiku-4-5-20251001",
    ),
    "security": ReviewerConfig(
        focus_area="focus: Check for security vulnerabilities, injection risks, and auth issues.",
        model_name="claude-sonnet-4-20250514",
    ),
    "performance": ReviewerConfig(
        focus_area="focus: Check for performance bottlenecks, unnecessary allocations, and O(n^2) loops.",
        model_name="claude-haiku-4-5-20251001",
    ),
}


# ---------------------------------------------------------------------------
# Random data generation
# ---------------------------------------------------------------------------

_LOREM = (
    "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod "
    "tempor incididunt ut labore et dolore magna aliqua ut enim ad minim veniam "
    "quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo "
    "consequat duis aute irure dolor in reprehenderit"
).split()

_FILE_PATHS = [
    "src/main.py",
    "src/auth/login.py",
    "src/api/handlers.py",
    "src/db/models.py",
    "src/utils/cache.py",
    "src/worker/tasks.py",
    "tests/test_api.py",
    "tests/test_auth.py",
    "lib/config.py",
    "lib/retry.py",
]

_SEVERITIES = ["nit", "suggestion", "blocking"]

_AUTHORS = ["alice", "bob", "carol", "dave"]


def _lorem_snippet(rng: random.Random, min_words: int = 8, max_words: int = 40) -> str:
    n = rng.randint(min_words, max_words)
    return " ".join(rng.choice(_LOREM) for _ in range(n))


def random_comment(rng: random.Random) -> ReviewComment:
    return ReviewComment(
        author=rng.choice(_AUTHORS),
        body=f"comment: {_lorem_snippet(rng)}",
        severity=rng.choice(_SEVERITIES),
    )


def random_review(rng: random.Random) -> CodeReview:
    n_comments = rng.randint(2, 8)
    return CodeReview(
        file_path=rng.choice(_FILE_PATHS),
        comments=[random_comment(rng) for _ in range(n_comments)],
    )


def random_summary(
    rng: random.Random,
    review_ref: EntityRef[CodeReview],
    config_ref: EntityRef[ReviewerConfig],
) -> ReviewSummary:
    score = round(rng.uniform(1.0, 10.0), 2)
    return ReviewSummary(
        review=review_ref,
        reviewer_config=config_ref,
        overall_score=score,
        summary_text=f"summary: {_lorem_snippet(rng, 10, 25)}",
    )


def generate_reviews() -> list[tuple[str, CodeReview]]:
    """Generate a fixed dataset of named code reviews.

    Uses a fixed seed so every call returns the same data.
    """
    rng = random.Random(123)
    return [(f"review_{i}", random_review(rng)) for i in range(20)]
