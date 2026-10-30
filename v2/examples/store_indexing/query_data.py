"""Query experiment data using sql() and iter_matches.

This script opens the namespace created by generate_data.py and demonstrates
two ways to query indexed data:

  1. sql() -- Run analytical SQL against virtual views built from indexer constraints.
     Each QueryView becomes a CTE (Common Table Expression) in the generated SQL.
     Good for aggregations, joins, and exploratory analysis.

  2. iter_matches() -- Iterate over individual matching entities with their binding
     variables. Good for loading and inspecting specific entities that match some
     condition.

Usage:
    uv run python -m examples.store_indexing.query_data /tmp/fxtr_example
    uv run python -m examples.store_indexing.query_data /tmp/fxtr_example --use-tsv-namespace
"""

from __future__ import annotations

import argparse
from pathlib import Path

import anyio

from fxtr2.data.entity_ref_scope import EntityRef, FxtrScope
from fxtr2.data.fxtr_context_api import (
    FxtrQueryableNamespace,
    SQLiteFxtrNamespace,
    TsvFxtrNamespace,
)
from fxtr2.data.store_indexing.indexers import TypedPathValueIndexer
from fxtr2.data.store_indexing.indexing_types import QueryView

from .entities import Experiment, Rollout, RolloutNote, TurnLabel

# Type IDs for !(as ...) / !(as? ...) type filtering in query expressions.
# These match the type IDs used in the indexer definitions in generate_data.py.
_EXP_TYPE = f"{Experiment.fxtr_type:q}"
_TURN_LABEL_TYPE = f"{TurnLabel.fxtr_type:q}"
_ROLLOUT_NOTE_TYPE = f"{RolloutNote.fxtr_type:q}"
_ROLLOUT_TYPE = f"{Rollout.fxtr_type:q}"

# ---------------------------------------------------------------------------
# Part 1: Analytical queries with sql()
# ---------------------------------------------------------------------------


async def run_sql_queries(ns: FxtrQueryableNamespace) -> None:
    """Demonstrate the sql() API for analytical queries.

    The key idea: each QueryView groups one or more IndexerConstraints into a
    virtual table (CTE). Constraints within the same view that share variable
    names (like $r) are automatically joined on those variables -- this is the
    "datalog-style unification" that makes it easy to correlate data from
    different indexers.
    """
    print("=" * 70)
    print("PART 1: Analytical SQL queries")
    print("=" * 70)

    # --- Query 1: Per-model average scores ---
    # This view has two columns from two different indexers, unified by sharing
    # no variables (the model indexer has none, the score indexer fans out over
    # rollout and metric). The result is a cross of (model, rollout, metric, score)
    # rows for each experiment.
    #
    # Note the !(as ...) strict type assertion on the experiment queries -- this
    # ensures only Experiment entities are indexed. If a non-Experiment were
    # pinned under "experiments:", it would raise an error at index time.
    print("\n--- Query 1: Average accuracy score by model ---")
    rows = await ns.sql(
        query="""
            SELECT model, ROUND(AVG(score), 3) AS avg_accuracy, COUNT(*) AS n_rollouts
            FROM experiment_data
            WHERE m = 'accuracy'
            GROUP BY model
            ORDER BY avg_accuracy DESC
        """,
        views={
            "experiment_data": QueryView(
                "experiments:",
                model=TypedPathValueIndexer.query(f"!(as {_EXP_TYPE}).config!.model_name: str"),
                score=TypedPathValueIndexer.query(
                    f"!(as {_EXP_TYPE}).rollouts[$r: int]!.scores[$m: str]: float"
                ),
            ),
        },
    )
    print(f"  {'Model':<25} {'Avg Accuracy':>14} {'Rollouts':>10}")
    print(f"  {'-'*25} {'-'*14} {'-'*10}")
    for row in rows:
        print(f"  {row['model']:<25} {row['avg_accuracy']:>14.3f} {row['n_rollouts']:>10}")

    # --- Query 2: Longest turns per experiment ---
    # Here we build a view with turn_length indexed by $r (rollout) and $t (turn).
    print("\n--- Query 2: Top 5 longest turns across all experiments ---")
    rows = await ns.sql(
        query="""
            SELECT pin_name, r, t, turn_length
            FROM experiment_turns
            ORDER BY turn_length DESC
            LIMIT 5
        """,
        views={
            "experiment_turns": QueryView(
                "experiments:",
                turn_length=TypedPathValueIndexer.query(
                    f"!(as {_EXP_TYPE}).rollouts[$r: int]!.turns[$t: int]!.length: int"
                ),
            ),
        },
    )
    print(f"  {'Experiment':<25} {'Rollout':>8} {'Turn':>6} {'Length':>8}")
    print(f"  {'-'*25} {'-'*8} {'-'*6} {'-'*8}")
    for row in rows:
        print(f"  {row['pin_name']:<25} {row['r']:>8} {row['t']:>6} {row['turn_length']:>8}")

    # --- Query 3: Join experiment data with labels via turn FxtrID ---
    # This is the showcase query. We build TWO views:
    #   - experiment_data: has turn_ref (FxtrID), score, model, etc.
    #   - label_data: has turn_ref (FxtrID), tag
    # Then we JOIN them on turn_ref to find labeled turns and their context.
    print("\n--- Query 3: Labeled turns with their experiment context ---")
    rows = await ns.sql(
        query="""
            SELECT
                e.pin_name AS experiment,
                e.model,
                e.r AS rollout,
                e.t AS turn,
                ROUND(e.score, 3) AS accuracy,
                l.tag
            FROM experiment_turns_with_ids e
            JOIN label_info l ON e.turn_ref = l.turn_ref
            WHERE e.m = 'accuracy'
            ORDER BY e.pin_name, e.r, e.t
        """,
        views={
            # Experiment view: scores + turn FxtrIDs, unified on $r.
            # $r appears in both score and turn_ref constraints, so they're joined
            # on rollout index. $t appears only in turn_ref. $m appears only in score.
            "experiment_turns_with_ids": QueryView(
                "experiments:",
                model=TypedPathValueIndexer.query(f"!(as {_EXP_TYPE}).config!.model_name: str"),
                score=TypedPathValueIndexer.query(
                    f"!(as {_EXP_TYPE}).rollouts[$r: int]!.scores[$m: str]: float"
                ),
                turn_ref=TypedPathValueIndexer.query(
                    f"!(as {_EXP_TYPE}).rollouts[$r: int]!(as {_ROLLOUT_TYPE}).turns[$t: int]:"
                    " FxtrID"
                ),
            ),
            # Label view: tag + turn FxtrID.
            # Uses !(as? ...) LAX filtering to only include TurnLabel entities,
            # silently skipping RolloutNotes that share the "labels:" prefix.
            "label_info": QueryView(
                "labels:",
                tag=TypedPathValueIndexer.query(f"!(as? {_TURN_LABEL_TYPE}).tag: str"),
                turn_ref=TypedPathValueIndexer.query(f"!(as? {_TURN_LABEL_TYPE}).turn: FxtrID"),
            ),
        },
    )
    if not rows:
        print("  (no labeled turns found)")
    else:
        print(f"  {'Experiment':<22} {'Model':<22} {'R':>3} {'T':>3} {'Accuracy':>10} {'Tag':<12}")
        print(f"  {'-'*22} {'-'*22} {'-'*3} {'-'*3} {'-'*10} {'-'*12}")
        for row in rows:
            print(
                f"  {row['experiment']:<22} {row['model']:<22} "
                f"{row['rollout']:>3} {row['turn']:>3} "
                f"{row['accuracy']:>10.3f} {row['tag']:<12}"
            )

    # --- Query 4: Rollout notes (demonstrating the other label type) ---
    # RolloutNotes and TurnLabels share the "labels:" prefix, but the !(as? ...)
    # filters ensure each view only sees its own entity type. This query shows
    # notes alongside the experiment's model and rollout scores.
    print("\n--- Query 4: Rollout notes with experiment context ---")
    rows = await ns.sql(
        query="""
            SELECT
                e.pin_name AS experiment,
                e.model,
                e.r AS rollout,
                ROUND(e.score, 3) AS accuracy,
                n.author,
                n.note
            FROM experiment_rollouts e
            JOIN note_info n ON e.rollout_ref = n.rollout_ref
            WHERE e.m = 'accuracy'
            ORDER BY e.pin_name, e.r
        """,
        views={
            "experiment_rollouts": QueryView(
                "experiments:",
                model=TypedPathValueIndexer.query(f"!(as {_EXP_TYPE}).config!.model_name: str"),
                score=TypedPathValueIndexer.query(
                    f"!(as {_EXP_TYPE}).rollouts[$r: int]!.scores[$m: str]: float"
                ),
                rollout_ref=TypedPathValueIndexer.query(
                    f"!(as {_EXP_TYPE}).rollouts[$r: int]: FxtrID"
                ),
            ),
            # Note view: uses !(as? ...) to filter to RolloutNote entities only.
            "note_info": QueryView(
                "labels:",
                note=TypedPathValueIndexer.query(f"!(as? {_ROLLOUT_NOTE_TYPE}).note: str"),
                author=TypedPathValueIndexer.query(f"!(as? {_ROLLOUT_NOTE_TYPE}).author: str"),
                rollout_ref=TypedPathValueIndexer.query(
                    f"!(as? {_ROLLOUT_NOTE_TYPE}).rollout: FxtrID"
                ),
            ),
        },
    )
    if not rows:
        print("  (no rollout notes found)")
    else:
        print(f"  {'Experiment':<22} {'Model':<22} {'R':>3} {'Accuracy':>10} {'Author':<8} Note")
        print(f"  {'-'*22} {'-'*22} {'-'*3} {'-'*10} {'-'*8} {'-'*40}")
        for row in rows:
            note_preview = row["note"][:40] + ("..." if len(row["note"]) > 40 else "")
            print(
                f"  {row['experiment']:<22} {row['model']:<22} "
                f"{row['rollout']:>3} {row['accuracy']:>10.3f} "
                f"{row['author']:<8} {note_preview}"
            )


# ---------------------------------------------------------------------------
# Part 2: Loading specific entities with iter_matches
# ---------------------------------------------------------------------------


async def run_iter_matches(ns: FxtrQueryableNamespace) -> None:
    """Demonstrate iter_matches for loading and inspecting matched entities.

    While sql() is great for aggregation, iter_matches gives you the actual
    entity IDs so you can load the full entity objects. Here we find experiments
    with high accuracy scores and load their rollout data.
    """
    print("\n" + "=" * 70)
    print("PART 2: Loading entities with iter_matches")
    print("=" * 70)

    # Find experiments where accuracy > 0.85, getting the rollout index.
    # Uses !(as ...) strict type assertion just like the sql() queries.
    score_constraint = TypedPathValueIndexer.query(
        f"!(as {_EXP_TYPE}).rollouts[$r: int]!.scores[$m: str]: float",
        where="$m = 'accuracy' AND value > 0.85",
    )

    print("\n--- Experiments with accuracy > 0.85 ---")
    print(f"  {'Pin Key':<25} {'Rollout':>8} {'Entity ID':<20}")
    print(f"  {'-'*25} {'-'*8} {'-'*20}")

    matches = []
    async for pin_key, entity_id, bindings in ns.iter_matches(
        score_constraint,
        key_prefix="experiments:",
    ):
        r = bindings["r"]
        print(f"  {pin_key:<25} {r:>8} {entity_id.summary():<20}")
        matches.append((pin_key, entity_id, r))

    # Now load a few of those experiments and print details about the matching rollouts.
    # We use a child scope that includes the namespace scope, so we can load entities
    # that were retrieved from the namespace.
    print(f"\n--- Loading details for {min(3, len(matches))} matches ---")

    async with FxtrScope(include=[ns.scope]):
        for pin_key, entity_id, rollout_idx in matches[:3]:
            # Create an EntityRef and load the full Experiment entity.
            # Child[ExperimentConfig] is auto-loaded, but ChildRef[Rollout] is lazy.
            exp_ref = EntityRef(entity_id, Experiment)
            exp = await exp_ref.load()

            # Load the specific rollout. Since rollouts are ChildRefs (lazy), we
            # call .load() explicitly to fetch the Rollout entity from the store.
            rollout: Rollout = await exp.rollouts[rollout_idx].load()

            print(f"\n  {pin_key} (rollout {rollout_idx}):")
            print(f"    Model: {exp.config.model_name}")
            print(f"    Temperature: {exp.config.temperature}")
            print(f"    Scores: { {k: round(v, 3) for k, v in rollout.scores.items()} }")
            print(f"    Turns ({len(rollout.turns)}):")

            # Load and print the first 2 turns from this rollout.
            for t_idx, turn_ref in enumerate(rollout.turns[:2]):
                turn = await turn_ref.load()
                print(f"      [{t_idx}] {turn.turn_id}: {turn.content[:60]}...")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main(db_dir: str, *, use_tsv_namespace: bool = False) -> None:
    db_subdir = Path(db_dir) / "demo_data"

    if use_tsv_namespace:
        tsv_namespace_dir = db_subdir / "tsv_namespace"
        print(f"Opening TsvFxtrNamespace at {tsv_namespace_dir}")
        async with TsvFxtrNamespace(tsv_namespace_dir) as ns:
            await run_sql_queries(ns)
            await run_iter_matches(ns)
    else:
        async with SQLiteFxtrNamespace(str(db_subdir / "store.sqlite"), "experiments") as ns:
            await run_sql_queries(ns)
            await run_iter_matches(ns)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Query experiment data from a namespace created by generate_data.",
    )
    parser.add_argument("db_dir", help="Directory containing the demo data.")
    parser.add_argument(
        "--use-tsv-namespace",
        action="store_true",
        help="Read from a TsvFxtrNamespace instead of plain SQLite.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    async def _run() -> None:
        await main(args.db_dir, use_tsv_namespace=args.use_tsv_namespace)

    anyio.run(_run)
