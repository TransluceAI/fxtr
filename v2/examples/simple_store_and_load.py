"""Guided tour of fxtr2's core features.

This script walks through the basics of defining entities, storing and loading them,
working with scopes, persisting data to a namespace, following references, inspecting
types, and querying with SQL indexes.

Run it with:
    cd v2
    uv run python examples/simple_store_and_load.py
"""

from __future__ import annotations

import dataclasses
import tempfile
import uuid
from pathlib import Path

import anyio

from fxtr2.conversion.converters.mashumaro_entities import (
    Child,
    ChildRef,
    DataclassEntity,
    EntityRef,
)
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import FxtrScope, get_active_scope
from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace
from fxtr2.data.store_indexing.indexers import TypedPathValueIndexer
from fxtr2.data.store_indexing.indexing_types import QueryView
from fxtr2.data.system_entities import FxtrBasicType, define_type

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Part 1: Defining entity types
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Every set of entity types lives under a namespace UUID to prevent type-name
# collisions between independent projects. Each project picks a random UUID once
# and re-uses it for all its types.

EXAMPLE_NS = uuid.UUID("e47ac10b-58cc-4372-a567-0e02b2c3d479")

# Most entities are frozen dataclasses. The `fxtr_type` argument registers a type ID
# derived from the namespace UUID and a name. This type ID becomes the entity's
# `$fxtr.type` field when serialized.
# (Note: It is also possible to define custom non-dataclass entities with custom serialization
# logic for advanced use cases.)


@dataclasses.dataclass(frozen=True)
class Author(DataclassEntity, fxtr_type=define_type(EXAMPLE_NS, "author")):
    """An author with a name and affiliation."""

    name: str
    affiliation: str


@dataclasses.dataclass(frozen=True)
class Paper(DataclassEntity, fxtr_type=define_type(EXAMPLE_NS, "paper")):
    """A paper with a title, an inline author, and scored reviews.

    - `author` is a Child[Author]: a strong reference that is automatically loaded
      when the Paper is deserialized. No separate `.load()` call needed.
    - `reviews` contains ChildRef[Review]: strong references that are lazy — you
      must call `.load()` explicitly to fetch each Review.
    """

    title: str
    year: int
    author: Child[Author]
    reviews: tuple[ChildRef[Review], ...]


@dataclasses.dataclass(frozen=True)
class Review(DataclassEntity, fxtr_type=define_type(EXAMPLE_NS, "review")):
    """A review with a score and comment."""

    score: float
    comment: str


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Part 2: Storing and loading entities
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def part2_store_and_load() -> None:
    print("=" * 70)
    print("Part 2: Storing and loading entities")
    print("=" * 70)

    # .store() serializes the entity to CBOR, computes a content hash, and returns
    # an EntityRef. The global scope is used by default.
    alice = Author(name="Alice", affiliation="MIT")
    alice_ref: EntityRef[Author] = alice.store()

    print(f"  Stored Author -> {alice_ref.entity_id.id_string()}")

    # Content-addressing means storing the same data gives the same ID.
    alice_ref_2 = Author(name="Alice", affiliation="MIT").store()
    assert alice_ref.entity_id == alice_ref_2.entity_id
    print(f"  Same data -> same ID: {alice_ref.entity_id == alice_ref_2.entity_id}")

    # .load() deserializes the entity from storage. It's async because it may need
    # to fetch data from a backing store.
    loaded_alice: Author = await alice_ref.load()
    print(f"  Loaded: {loaded_alice}")
    assert loaded_alice.name == "Alice"

    # Build a small graph: reviews stored first, then a paper that references them.
    r1_ref = Review(score=8.5, comment="Strong methodology").store()
    r2_ref = Review(score=7.0, comment="Needs more baselines").store()

    paper = Paper(
        title="Content-Addressed Everything",
        year=2025,
        author=alice,  # Child[Author] — stored inline
        reviews=(r1_ref, r2_ref),  # ChildRef[Review] — stored as lazy references
    )
    paper_ref = paper.store()
    print(f"  Stored Paper -> {paper_ref.entity_id.id_string()}")

    # Load the paper. Child[Author] is auto-loaded, ChildRef[Review] is lazy.
    loaded_paper: Paper = await paper_ref.load()
    print(f"  Paper title: {loaded_paper.title}")
    print(f"  Author (auto-loaded): {loaded_paper.author.name}")

    # ChildRefs require an explicit .load() call.
    review_1: Review = await loaded_paper.reviews[0].load()
    review_2: Review = await loaded_paper.reviews[1].load()
    print(f"  Review 1: {review_1.score} — {review_1.comment}")
    print(f"  Review 2: {review_2.score} — {review_2.comment}")
    print()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Part 3: Scopes and lifetime management
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def part3_scopes() -> None:
    print("=" * 70)
    print("Part 3: Scopes and lifetime management")
    print("=" * 70)

    # By default, new scopes inherit from the active scope. When a scope exits,
    # entities only tracked in that scope become inaccessible.

    paper_ref: EntityRef[Paper] | None = None

    async with FxtrScope() as inner:
        r_ref = Review(score=9.0, comment="Excellent").store()
        paper_ref = Paper(
            title="Temporary Paper",
            year=2025,
            author=Author(name="Bob", affiliation="Stanford"),
            reviews=(r_ref,),
        ).store()

        # Works inside the scope:
        loaded = await paper_ref.load()
        print(f"  Inside scope: loaded '{loaded.title}'")

        # To keep data alive beyond this scope, copy it to the outer scope:
        # await paper_ref.copy_to(inner.outer)
        # (We intentionally don't copy here, to show the cleanup.)

    # After the scope exits, the entity is no longer loadable from memory.
    try:
        await paper_ref.load()
        print("  ERROR: should not reach here")
    except ValueError as e:
        print(f"  After scope exit: load raised ValueError (entity cleaned up)")

    # Demonstrate copy_to: this time, copy the result out before the scope exits.
    async with FxtrScope() as inner:
        kept_ref = Review(score=10.0, comment="Perfect").store()
        await kept_ref.copy_to(inner.outer)

    # Still loadable after the inner scope exits!
    kept_review = await kept_ref.load()
    print(f"  Copied out: score={kept_review.score} — {kept_review.comment}")
    print()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Part 4: Persisting to a namespace
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def part4_namespace(db_path: str) -> None:
    print("=" * 70)
    print("Part 4: Persisting to a namespace")
    print("=" * 70)

    # A namespace gives entities human-readable names ("pins") and persists them.
    # SQLiteFxtrNamespace stores everything in a local SQLite database.

    # Store some papers into a namespace.
    async with SQLiteFxtrNamespace(db_path, "papers") as ns:
        r1 = Review(score=9.0, comment="Groundbreaking").store()
        r2 = Review(score=6.5, comment="Incremental").store()
        r3 = Review(score=8.0, comment="Well-written").store()

        paper_a = Paper(
            title="On Content Addressing",
            year=2024,
            author=Author(name="Alice", affiliation="MIT"),
            reviews=(r1, r2),
        ).store()

        paper_b = Paper(
            title="Provenance Tracking at Scale",
            year=2025,
            author=Author(name="Bob", affiliation="Stanford"),
            reviews=(r3,),
        ).store()

        # Pinning copies the entity (and its full subgraph) into the namespace.
        await ns.pin("papers:content_addressing", paper_a)
        await ns.pin("papers:provenance", paper_b)

        pins = await ns.list_pins()
        print(f"  Pinned {len(pins)} entities: {list(pins.keys())}")

    # Close and re-open — data persists on disk.
    async with SQLiteFxtrNamespace(db_path, "papers") as ns:
        retrieved_ref = await ns.retrieve("papers:content_addressing", Paper)
        paper = await retrieved_ref.load()
        print(f"  Retrieved from disk: '{paper.title}' by {paper.author.name}")

        # Follow lazy references from the retrieved entity.
        for i, review_ref in enumerate(paper.reviews):
            review = await review_ref.load()
            print(f"    Review {i+1}: {review.score} — {review.comment}")

    print()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Part 5: Inspecting $fxtr.type
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def part5_inspect_type(db_path: str) -> None:
    print("=" * 70)
    print("Part 5: Inspecting $fxtr.type")
    print("=" * 70)

    # Every entity has a $fxtr.type field that is itself a FxtrID pointing to a
    # type definition entity. You can load and inspect it.

    async with SQLiteFxtrNamespace(db_path, "papers") as ns:
        paper_ref = await ns.retrieve("papers:content_addressing", Paper)

        # Load raw CBOR to see the $fxtr.type field.
        scope = get_active_scope()
        raw_cbor = await scope.load_cbor(paper_ref.entity_id)
        type_id: FxtrID = raw_cbor["$fxtr.type"].inner_id
        print(f"  Paper's $fxtr.type ID: {type_id.id_string()}")

        # The type ID points to a FxtrBasicType entity that carries the type name
        # (and namespace UUID). Because types are themselves entities, they could
        # carry richer data in the future: schemas, trait declarations,
        # visualization instructions, etc.
        type_entity: FxtrBasicType = await EntityRef(type_id, FxtrBasicType).load()
        print(f"  Type name: {type_entity.type_name}")
        print(f"  Namespace UUID: {type_entity.namespace_uuid}")

        # You can also access the type ID directly from the Python class:
        print(f"  Paper.fxtr_type == type_id: {Paper.fxtr_type == type_id}")

    print()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Part 6: Querying with SQL indexes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def part6_indexing(db_path: str) -> None:
    print("=" * 70)
    print("Part 6: Querying with SQL indexes")
    print("=" * 70)

    # The type ID as a quoted string, used in path expressions for type filtering.
    paper_type = f"{Paper.fxtr_type:q}"
    review_type = f"{Review.fxtr_type:q}"

    async with SQLiteFxtrNamespace(db_path, "papers") as ns:
        # Register indexers that define how to walk through the entity graph.
        # Path expressions describe the traversal:
        #   "!" dereferences a link (loads the referenced entity)
        #   "!(as '<type>')" also asserts the entity has the expected type
        #   ".field" accesses a field
        #   "[$var: type]" iterates over a list/dict, binding a variable
        #   ": type" specifies the leaf value type

        # Index the paper title.
        await ns.register_indexer(
            TypedPathValueIndexer.parse(f"!(as {paper_type}).title: str"),
            key_prefix="papers:",
        )

        # Index the paper year.
        await ns.register_indexer(
            TypedPathValueIndexer.parse(f"!(as {paper_type}).year: int"),
            key_prefix="papers:",
        )

        # Index review scores by following the paper -> reviews -> score path.
        # [*: int] iterates over the reviews tuple (unnamed variable in parse mode).
        # The second "!" follows the ChildRef to load each Review entity.
        # !(as '<review_type>') asserts the loaded entity is a Review.
        await ns.register_indexer(
            TypedPathValueIndexer.parse(
                f"!(as {paper_type}).reviews[*: int]!(as {review_type}).score: float"
            ),
            key_prefix="papers:",
        )

        # Index author names by following the paper -> author (inline Child) path.
        # The "!" after "author" dereferences the Child link. Since Child[Author]
        # is auto-loaded during deserialization, it still needs to be dereferenced
        # explicitly in path expressions (which operate on the serialized form).
        await ns.register_indexer(
            TypedPathValueIndexer.parse(f"!(as {paper_type}).author!.name: str"),
            key_prefix="papers:",
        )

        print("  Registered 4 indexers on 'papers:' prefix\n")

        # --- Query 1: List all papers with their authors ---
        print("  --- All papers ---")
        rows = await ns.sql(
            query="""
                SELECT pin_name, title, year, author_name
                FROM paper_info
                ORDER BY year
            """,
            views={
                "paper_info": QueryView(
                    "papers:",
                    title=TypedPathValueIndexer.query(f"!(as {paper_type}).title: str"),
                    year=TypedPathValueIndexer.query(f"!(as {paper_type}).year: int"),
                    author_name=TypedPathValueIndexer.query(
                        f"!(as {paper_type}).author!.name: str"
                    ),
                ),
            },
        )
        for row in rows:
            print(
                f"    {row['pin_name']}: '{row['title']}' ({row['year']}) by {row['author_name']}"
            )

        # --- Query 2: Average review score per paper ---
        # This traverses paper -> reviews[] -> score, grouping by paper.
        # The $r variable (review index) fans out into multiple rows per paper,
        # which we then aggregate.
        print("\n  --- Average review scores ---")
        rows = await ns.sql(
            query="""
                SELECT title, ROUND(AVG(score), 1) AS avg_score, COUNT(*) AS n_reviews
                FROM paper_reviews
                GROUP BY title
                ORDER BY avg_score DESC
            """,
            views={
                "paper_reviews": QueryView(
                    "papers:",
                    title=TypedPathValueIndexer.query(f"!(as {paper_type}).title: str"),
                    score=TypedPathValueIndexer.query(
                        f"!(as {paper_type}).reviews[$r: int]!(as {review_type}).score: float"
                    ),
                ),
            },
        )
        for row in rows:
            print(f"    '{row['title']}': avg={row['avg_score']}, n={row['n_reviews']}")

    print()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Main
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def main() -> None:
    await part2_store_and_load()
    await part3_scopes()

    # Parts 4-6 use a temporary directory for the SQLite database.
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "example.sqlite")

        await part4_namespace(db_path)
        await part5_inspect_type(db_path)
        await part6_indexing(db_path)

    print("Done! All parts completed successfully.")


if __name__ == "__main__":
    anyio.run(main)
