"""Entity types for the store indexing example.

This file defines a small domain model for tracking ML experiments. The entities form
a hierarchy:

    Experiment
      ├── config: ExperimentConfig    (inline child -- auto-loaded with the experiment)
      └── rollouts: [Rollout, ...]    (child refs -- loaded on demand)
              └── turns: [Turn, ...]  (child refs -- loaded on demand)

Separately, two kinds of annotations live under the "labels:" prefix:
- TurnLabels point at specific turns via a ChildRef
- RolloutNotes attach freeform notes to rollouts

Since both share the same prefix, our indexers use type filtering (!(as? ...)) to
index only the relevant entity type. Labels are pinned independently from experiments,
so to connect them we JOIN on the Turn's or Rollout's entity ID in SQL queries.

Key concepts shown here:
- Child[T]: An inline child entity. Stored as a separate content-addressed entity, but
  automatically loaded when the parent is deserialized. Good for small, always-needed data.
- ChildRef[T]: A lazy reference to a child entity. The child is a separate entity that
  must be explicitly loaded via .load(). Good for large or optional sub-graphs.
- All entities are frozen dataclasses and immutable (content-addressed by their hash).
"""

from __future__ import annotations

import dataclasses
import uuid

from frozendict import frozendict

from fxtr2.conversion.converters.mashumaro_entities import Child, ChildRef, DataclassEntity
from fxtr2.data.system_entities import define_type

# Every set of entity types lives under a namespace UUID to avoid collisions.
EXAMPLE_UUID = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef0123456789")


@dataclasses.dataclass(frozen=True)
class ExperimentConfig(
    DataclassEntity,
    fxtr_type=define_type(EXAMPLE_UUID, "experiment_config"),
):
    """Configuration for an experiment run.

    This is stored as a Child[ExperimentConfig] on Experiment, meaning it gets
    automatically loaded whenever you load an Experiment -- no extra .load() call needed.
    """

    model_name: str
    temperature: float
    seed: int


@dataclasses.dataclass(frozen=True)
class Turn(
    DataclassEntity,
    fxtr_type=define_type(EXAMPLE_UUID, "turn"),
):
    """A single turn (interaction step) within a rollout.

    Stored as a ChildRef on Rollout, so you must call .load() to access it.
    Each turn has a unique turn_id and some content with a character length.
    """

    turn_id: str
    timestamp: str
    length: int
    content: str


@dataclasses.dataclass(frozen=True)
class Rollout(
    DataclassEntity,
    fxtr_type=define_type(EXAMPLE_UUID, "rollout"),
):
    """A single rollout (sequence of turns) within an experiment.

    Stored as a ChildRef on Experiment. Contains references to its turns
    (also ChildRefs) and a frozendict of metric scores. We use frozendict instead
    of dict so the entity is properly immutable and can be cached after loading.
    """

    turns: tuple[ChildRef[Turn], ...]
    scores: frozendict[str, float]


@dataclasses.dataclass(frozen=True)
class Experiment(
    DataclassEntity,
    fxtr_type=define_type(EXAMPLE_UUID, "experiment"),
):
    """A complete experiment with a config and a list of rollouts.

    - config is a Child (inline, auto-loaded)
    - rollouts are ChildRefs (lazy, loaded on demand)

    Pinned under keys like "experiments:exp_001".
    """

    config: Child[ExperimentConfig]
    rollouts: tuple[ChildRef[Rollout], ...]


@dataclasses.dataclass(frozen=True)
class TurnLabel(
    DataclassEntity,
    fxtr_type=define_type(EXAMPLE_UUID, "turn_label"),
):
    """A label attached to a specific turn.

    The `turn` field is a ChildRef pointing at the labeled Turn entity. Since labels
    are pinned separately from experiments (under "labels:label_001"), the only way
    to connect a label to its experiment context is by JOINing on the Turn's entity ID.

    This is a great showcase of the sql() API: we can build one view over experiment
    data and another over labels, then JOIN them on the turn's FxtrID.
    """

    turn: ChildRef[Turn]
    tag: str


@dataclasses.dataclass(frozen=True)
class RolloutNote(
    DataclassEntity,
    fxtr_type=define_type(EXAMPLE_UUID, "rollout_note"),
):
    """A freeform note attached to an entire rollout.

    Also pinned under "labels:" alongside TurnLabels. Since these two entity types
    share a prefix, indexers that only care about TurnLabels use !(as? ...) lax type
    filtering to silently skip RolloutNotes (and vice versa). This is the "lax"
    variant -- non-matching entities are skipped instead of raising an error.
    """

    rollout: ChildRef[Rollout]
    note: str
    author: str
