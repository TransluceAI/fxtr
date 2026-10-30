"""Generate random experiment data and store it in a namespace.

This script creates a small dataset of experiments, each with rollouts and turns,
plus some labels on specific turns. It then registers indexers so the data can be
queried efficiently.

Usage:
    uv run python -m examples.store_indexing.generate_data /tmp/fxtr_example
    uv run python -m examples.store_indexing.generate_data /tmp/fxtr_example --use-tsv-namespace

What this script does:
  1. Defines entity types (from entities.py) representing experiments with configs,
     rollouts, turns, and labels.
  2. Generates ~10 experiments with random data, pins them at "experiments:<name>".
  3. Generates ~10 TurnLabels and ~5 RolloutNotes, all under "labels:<id>".
  4. Registers indexers using TypedPathValueIndexer.parse() -- these define how the
     indexing system should walk through the entity graph to extract queryable values.
     Some indexers use type filtering (!(as ...) and !(as? ...)) to handle multiple
     entity types sharing a prefix.

By default a SQLite-backed namespace is used. Pass --use-tsv-namespace to instead
create a git repository in the output directory and back the namespace with
TsvFxtrNamespace (git-tracked TSV files + SQLite blob storage).
"""

from __future__ import annotations

import argparse
import random
import shutil
from datetime import datetime, timezone
from pathlib import Path

import anyio
from frozendict import frozendict

from fxtr2.data.fxtr_context_api import (
    FxtrQueryableNamespace,
    SQLiteFxtrNamespace,
    TsvFxtrNamespace,
)
from fxtr2.data.store_indexing.indexers import TypedPathValueIndexer

from .entities import Experiment, ExperimentConfig, Rollout, RolloutNote, Turn, TurnLabel

# ---------------------------------------------------------------------------
# Random data generation helpers
# ---------------------------------------------------------------------------

MODEL_NAMES = ["gpt-4o", "claude-3.5-sonnet", "llama-3-70b", "gemini-1.5-pro"]
LABEL_TAGS = ["interesting", "error", "perfect", "off-topic", "verbose"]
NOTE_AUTHORS = ["alice", "bob", "carol"]
NOTE_TEMPLATES = [
    "Rollout shows strong performance on factual questions.",
    "Model struggles with multi-step reasoning here.",
    "Good example of consistent tone throughout.",
    "Notable hallucination in the middle turns.",
    "Interesting failure mode -- worth investigating.",
]

# Some canned turn content for variety.
TURN_TEMPLATES = [
    "The model responded with a detailed explanation of {topic}.",
    "User asked about {topic} and received a short answer.",
    "A follow-up question about {topic} led to a longer discussion.",
    "The assistant provided code examples related to {topic}.",
    "The conversation shifted to a tangential point about {topic}.",
]
TOPICS = [
    "sorting algorithms",
    "transformer architectures",
    "async I/O",
    "graph databases",
    "CBOR encoding",
]


def _random_turn(turn_idx: int, rollout_idx: int, exp_name: str) -> Turn:
    """Generate a random Turn entity."""
    template = random.choice(TURN_TEMPLATES)
    topic = random.choice(TOPICS)
    content = template.format(topic=topic)
    return Turn(
        turn_id=f"{exp_name}_r{rollout_idx}_t{turn_idx}",
        timestamp=datetime.now(timezone.utc).isoformat(),
        length=len(content),
        content=content,
    )


def _random_rollout(rollout_idx: int, exp_name: str, num_turns: int) -> tuple[Rollout, list[Turn]]:
    """Generate a random Rollout with its Turn entities.

    Returns both the Rollout (which holds ChildRefs to the turns) and the Turn
    objects themselves (so the caller can store them first).
    """
    turns = [_random_turn(t, rollout_idx, exp_name) for t in range(num_turns)]
    # Store turns and collect refs. (The actual .store() call happens in the
    # async main function where we have an active scope.)
    turn_refs = tuple(turn.store() for turn in turns)

    scores = frozendict(
        {
            "accuracy": round(random.uniform(0.5, 1.0), 3),
            "f1": round(random.uniform(0.4, 0.95), 3),
            "coherence": round(random.uniform(0.3, 1.0), 3),
        }
    )
    rollout = Rollout(turns=turn_refs, scores=scores)
    return rollout, turns


def _random_experiment(exp_name: str) -> tuple[Experiment, list[Rollout], list[Turn]]:
    """Generate a random Experiment with its Rollouts and Turns.

    Returns all three levels so the caller can track which turns belong to which
    experiments (needed for creating labels).
    """
    config = ExperimentConfig(
        model_name=random.choice(MODEL_NAMES),
        temperature=round(random.choice([0.0, 0.3, 0.7, 1.0]), 1),
        seed=random.randint(0, 9999),
    )

    num_rollouts = random.randint(2, 5)
    all_rollouts: list[Rollout] = []
    all_turns: list[Turn] = []

    for r in range(num_rollouts):
        num_turns = random.randint(10, 15)
        rollout, turns = _random_rollout(r, exp_name, num_turns)
        all_rollouts.append(rollout)
        all_turns.extend(turns)

    rollout_refs = tuple(r.store() for r in all_rollouts)
    experiment = Experiment(config=config, rollouts=rollout_refs)
    return experiment, all_rollouts, all_turns


# ---------------------------------------------------------------------------
# Indexer definitions
# ---------------------------------------------------------------------------

# These path expressions tell the indexer how to walk through the entity graph
# to extract values. The "!" markers follow FxtrID references (both strong and
# weak). Variables like [*: int] fan out over list indices or dict keys.
#
# For example, "!.rollouts[*: int]!.scores[*: str]: float" means:
#   1. "!" -- dereference the pinned entity (look at its CBOR)
#   2. ".rollouts" -- access the "rollouts" field
#   3. "[*: int]" -- iterate over the list (binding an int index variable)
#   4. "!" -- follow each rollout's ChildRef to load the Rollout entity
#   5. ".scores" -- access the "scores" dict
#   6. "[*: str]" -- iterate over dict keys (binding a str variable)
#   7. ": float" -- the value at the end of the path is a float
#
# Type filtering with !(as ...) and !(as? ...):
#   Any dereference "!" can include a type assertion. This is useful when a prefix
#   contains multiple entity types. Two variants:
#
#   !(as '<type_id>')   -- STRICT: requires the entity to match. Raises ValueError
#                          if the entity has a different type. Use when you know
#                          exactly what types live under the prefix.
#
#   !(as? '<type_id>')  -- LAX: silently skips non-matching entities (returns []
#                          from unpack_entity). Use when a prefix has mixed types
#                          and you only want to index some of them.

# We need the type IDs as quoted strings for the parse expressions. The :q format
# specifier on FxtrID produces the right form (e.g. "'d37v3w2o...'").
_EXP_TYPE = f"{Experiment.fxtr_type:q}"
_ROLLOUT_TYPE = f"{Rollout.fxtr_type:q}"
_TURN_LABEL_TYPE = f"{TurnLabel.fxtr_type:q}"
_ROLLOUT_NOTE_TYPE = f"{RolloutNote.fxtr_type:q}"

INDEXER_DEFINITIONS = {
    # -- Experiment indexers (prefix "experiments:") --
    # These use !(as <type>) for STRICT type checking. Since only Experiment entities
    # are pinned under "experiments:", this is an assertion that catches mistakes --
    # if something else accidentally ends up here, the indexer will error loudly.
    "config_model": TypedPathValueIndexer.parse(f"!(as {_EXP_TYPE}).config!.model_name: str"),
    "rollout_score": TypedPathValueIndexer.parse(
        f"!(as {_EXP_TYPE}).rollouts[*: int]!.scores[*: str]: float"
    ),
    "turn_length": TypedPathValueIndexer.parse(
        f"!(as {_EXP_TYPE}).rollouts[*: int]!.turns[*: int]!.length: int"
    ),
    # Extract the FxtrID of each turn. This is the key to JOINing with turn labels!
    # The value is the FxtrID (stored as a BLOB), which we can match against labels.
    # Here we also show that it is possible to use a type assertion in a dereference in the middle
    # of a path expression.
    "turn_ref": TypedPathValueIndexer.parse(
        f"!(as {_EXP_TYPE}).rollouts[*: int]!(as {_ROLLOUT_TYPE}).turns[*: int]: FxtrID"
    ),
    # Extract the FxtrID of each rollout. Used for JOINing with rollout notes.
    "rollout_ref": TypedPathValueIndexer.parse(f"!(as {_EXP_TYPE}).rollouts[*: int]: FxtrID"),
    # -- TurnLabel indexers (prefix "labels:") --
    # These use !(as? <type>) for LAX type filtering. The "labels:" prefix contains
    # both TurnLabel and RolloutNote entities. The lax filter silently skips
    # RolloutNotes (returning no index rows for them) instead of erroring.
    "label_tag": TypedPathValueIndexer.parse(f"!(as? {_TURN_LABEL_TYPE}).tag: str"),
    "label_turn_ref": TypedPathValueIndexer.parse(f"!(as? {_TURN_LABEL_TYPE}).turn: FxtrID"),
    # -- RolloutNote indexers (prefix "labels:") --
    # Same idea: lax filter skips TurnLabels when indexing RolloutNotes.
    "note_text": TypedPathValueIndexer.parse(f"!(as? {_ROLLOUT_NOTE_TYPE}).note: str"),
    "note_author": TypedPathValueIndexer.parse(f"!(as? {_ROLLOUT_NOTE_TYPE}).author: str"),
    "note_rollout_ref": TypedPathValueIndexer.parse(f"!(as? {_ROLLOUT_NOTE_TYPE}).rollout: FxtrID"),
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def _init_tsv_namespace(store_dir: Path) -> TsvFxtrNamespace:
    """Create a fresh git repo and initialize a TsvFxtrNamespace in it."""
    store_dir.mkdir(parents=True, exist_ok=True)
    await anyio.run_process(["git", "init"], cwd=store_dir)
    await anyio.run_process(["git", "config", "user.email", "demo@example.com"], cwd=store_dir)
    await anyio.run_process(["git", "config", "user.name", "Demo User"], cwd=store_dir)
    tsv_ns = TsvFxtrNamespace(store_dir)
    await tsv_ns.create(name="store_indexing_demo", description="Store indexing example")
    return tsv_ns


async def _populate_namespace(ns: FxtrQueryableNamespace) -> None:
    """Generate random data, pin it, and register indexers in *ns*."""
    # -- Generate and pin experiments --
    experiment_names = [f"exp_{i:03d}" for i in range(10)]
    # Track turns and rollouts for creating labels/notes later.
    all_turn_info: list[tuple[str, Turn]] = []  # (experiment_pin, turn)
    all_rollouts_by_exp: list[list[Rollout]] = []

    print("Generating experiments...")
    for name in experiment_names:
        experiment, rollouts, turns = _random_experiment(name)
        pin_key = f"experiments:{name}"
        await ns.pin(pin_key, experiment)
        for turn in turns:
            all_turn_info.append((pin_key, turn))
        all_rollouts_by_exp.append(rollouts)
        print(
            f"  Pinned {pin_key}: {experiment.config.model_name}, "
            f"{len(rollouts)} rollouts, {len(turns)} turns"
        )

    # -- Generate and pin TurnLabels --
    # TurnLabels point at specific turns and assign a tag.
    print("\nGenerating turn labels...")
    num_labels = 10
    labeled_turns = random.sample(all_turn_info, min(num_labels, len(all_turn_info)))
    for i, (_exp_pin, turn) in enumerate(labeled_turns):
        tag = random.choice(LABEL_TAGS)
        # Get the Turn's EntityRef (it was already stored, so .store() returns
        # the same ref thanks to content-addressing).
        turn_ref = turn.store()
        label = TurnLabel(turn=turn_ref, tag=tag)
        pin_key = f"labels:turn_label_{i:03d}"
        await ns.pin(pin_key, label)
        print(f"  Pinned {pin_key}: tag={tag!r}, turn={turn.turn_id}")

    # -- Generate and pin RolloutNotes --
    # RolloutNotes are a DIFFERENT entity type that also lives under "labels:".
    # This demonstrates type filtering: the TurnLabel indexers use !(as? ...)
    # to skip these, and vice versa.
    print("\nGenerating rollout notes...")
    all_rollout_info = [
        (rollout, exp_name)
        for exp_name, rollouts in zip(experiment_names, all_rollouts_by_exp)
        for rollout in rollouts
    ]
    num_notes = 5
    noted_rollouts = random.sample(all_rollout_info, min(num_notes, len(all_rollout_info)))
    for i, (rollout, exp_name) in enumerate(noted_rollouts):
        rollout_ref = rollout.store()
        note = RolloutNote(
            rollout=rollout_ref,
            note=random.choice(NOTE_TEMPLATES),
            author=random.choice(NOTE_AUTHORS),
        )
        pin_key = f"labels:rollout_note_{i:03d}"
        await ns.pin(pin_key, note)
        print(f"  Pinned {pin_key}: author={note.author!r}, exp={exp_name}")

    # -- Register indexers --
    # Each indexer is registered at a key prefix, which tells the system to only
    # index entities pinned under that prefix. This is both a performance
    # optimization and a correctness measure (different prefixes may have
    # different entity types).
    print("\nRegistering indexers...")
    experiment_indexers = [
        "config_model",
        "rollout_score",
        "turn_length",
        "turn_ref",
        "rollout_ref",
    ]
    for name in experiment_indexers:
        await ns.register_indexer(INDEXER_DEFINITIONS[name], key_prefix="experiments:")
        print(f"  Registered {name!r} on prefix 'experiments:'")

    # All label indexers share the "labels:" prefix. The !(as? ...) type
    # filters ensure each indexer only processes its own entity type.
    label_indexers = [
        "label_tag",
        "label_turn_ref",
        "note_text",
        "note_author",
        "note_rollout_ref",
    ]
    for name in label_indexers:
        await ns.register_indexer(INDEXER_DEFINITIONS[name], key_prefix="labels:")
        print(f"  Registered {name!r} on prefix 'labels:'")

    print(
        f"\n  {len(experiment_names)} experiments, {len(labeled_turns)} turn labels,"
        f" {len(noted_rollouts)} rollout notes"
    )
    print(f"  {len(INDEXER_DEFINITIONS)} indexers registered")


async def main(db_dir: str, *, use_tsv_namespace: bool = False) -> None:
    random.seed(42)  # reproducible output

    db_subdir = Path(db_dir) / "demo_data"
    shutil.rmtree(db_subdir, ignore_errors=True)
    db_subdir.mkdir(parents=True, exist_ok=True)

    if use_tsv_namespace:
        tsv_namespace_dir = db_subdir / "tsv_namespace"
        tsv_ns = await _init_tsv_namespace(tsv_namespace_dir)
        print(f"Using TsvFxtrNamespace at {tsv_namespace_dir}")
        async with tsv_ns as ns:
            await _populate_namespace(ns)
            print(f"\nDone! Data stored in {tsv_namespace_dir} (tsv namespace)")
    else:
        print(f"Using SQLiteFxtrNamespace at {db_subdir}")
        async with SQLiteFxtrNamespace(str(db_subdir / "store.sqlite"), "experiments") as ns:
            await _populate_namespace(ns)
            print(f"\nDone! Data stored in {db_subdir} (sqlite-backed)")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate random experiment data and store it in a namespace.",
    )
    parser.add_argument("db_dir", help="Directory to store the demo data in.")
    parser.add_argument(
        "--use-tsv-namespace",
        action="store_true",
        help="Back the namespace with a git repo + TsvFxtrNamespace instead of plain SQLite.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    async def _run() -> None:
        await main(args.db_dir, use_tsv_namespace=args.use_tsv_namespace)

    anyio.run(_run)
