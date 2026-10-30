"""Seed the demo's ``Note`` inputs into a scaffolded repo's entity store.

The committed ``publishing_workdir/root.fxtr.tsv`` carries the notes'
content-addressed ids (the git-tracked namespace *view*); this writes their
**bytes** into the local repo entity store (``.fxtr/``, gitignored) so the
preload FODs can fetch them via the manager's build-time rehost. The note content
here is the source of truth for those ids, so :func:`seed` asserts the ids it
produces match the committed TSV — a content edit that wasn't re-pinned fails
loudly (reprint with
``python -c "import demo_types; print(demo_types.Note(title=..., body=...))"``
stored in a scope, or just run this and read the error).

Reused by :mod:`scaffold_workdir` and the E2E test.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.repo_storage import RepoEntityStore
from fxtr2.data.tsv_namespace.tsv_file import read_tsv

from demo_types import Note

#: The seeded notes, keyed by their tsv pin key. Fixed content → the ids in
#: ``publishing_workdir/root.fxtr.tsv`` are committed; edit a note here only
#: together with re-pinning that TSV (:func:`seed` enforces the match).
NOTES: dict[str, Note] = {
    "note_0": Note(
        title="Adoption climbed in Q2",
        body="Weekly active users rose 18% quarter-over-quarter, led by the mobile client.",
    ),
    "note_1": Note(
        title="Latency regressed after the storage migration",
        body="p99 read latency moved from 120ms to 310ms the week of the cutover.",
    ),
    "note_2": Note(
        title="Support volume held flat",
        body="Ticket counts were unchanged despite the larger user base, suggesting better self-serve docs.",
    ),
}


async def seed(repo_path: Path) -> None:
    """Write the demo notes into ``repo_path``'s repo entity store (create if absent).

    Asserts each note's content-addressed id matches the committed
    ``root.fxtr.tsv`` pin before writing, so the namespace view and the seeded
    bytes can never silently diverge.
    """
    repo_path = Path(repo_path)
    committed = read_tsv(repo_path / "root.fxtr.tsv")

    async with FxtrScope(name="publishing-demo-seed") as scope:
        refs = {key: scope.store(note) for key, note in NOTES.items()}

        for key, ref in refs.items():
            want = committed.get(key)
            if want is None:
                raise SystemExit(f"seed_data: root.fxtr.tsv has no pin {key!r}")
            if want != ref.entity_id:
                raise SystemExit(
                    f"seed_data: id drift for {key!r} — root.fxtr.tsv pins "
                    f"{want.id_string()} but the note content produces "
                    f"{ref.entity_id.id_string()}. Re-pin the TSV."
                )

        async with RepoEntityStore.open(repo_path, create=True) as store:
            for ref in refs.values():
                await store.write_from_arena(
                    ref.entity_id, scope._main_arena, scope._current_auth_lease().clone()
                )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed the publishing demo's Note inputs into a scaffolded repo."
    )
    parser.add_argument("repo", type=Path, help="the scaffolded workdir repo path")
    args = parser.parse_args()
    asyncio.run(seed(args.repo))
    print(f"seeded {len(NOTES)} notes into {args.repo}/.fxtr")


if __name__ == "__main__":
    main()
