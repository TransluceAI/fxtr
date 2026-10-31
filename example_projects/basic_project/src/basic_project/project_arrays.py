"""Project arrays, from a script or notebook: declaring them, filling them, taking snapshots, and
passing a snapshot to a job.

The project arrays are the project's editable arrays: named, typed arrays in the project's database
that a user fills over time. A launch snapshots one and passes the snapshot as a job input. This
walkthrough keeps an array of notes about topics, where each note is an entity and the array maps
topics to note ids, and shows the round trip a user goes through, all of it through the
:class:`~fxtr.project.client.ProjectClient`:

1. declare a project array with its type;
2. store entities and upsert an array referencing them (persisted together);
3. load the array back and follow a reference;
4. take a snapshot, edit the array, snapshot again, and see how snapshots relate;
5. pass a snapshot to a job as its input and run the job.

``launch_project_arrays.py`` at the project root runs it against the project's database.
"""

from __future__ import annotations

import dataclasses
from typing import Annotated

from fxtr.core.entities import BoundID
from fxtr.entity_defns.array import Array
from fxtr.entity_defns.dataclass_entity import DataclassEntity
from fxtr.entity_defns.schema import IdSchema
from fxtr.experiment.handles import ArrayHandle
from fxtr.experiment.steps import StepContext, step
from fxtr.experiment.workflows import WorkflowContext, workflow
from fxtr.project.client import ProjectClient
from fxtr.project.formatting import format_array, format_type
from fxtr.runner import nodes

NOTE = "org.example.project_arrays.Note"


@dataclasses.dataclass(frozen=True)
class Note(DataclassEntity, fxtr_type=NOTE):
    """A note about a topic."""

    title: str
    body: str


NOTES_TYPE = ("[topic: str]", IdSchema(NOTE))
"""The project array's type: one note id per topic."""


@step(name="note_title")
async def note_title(context: StepContext, note: Note) -> str:
    """The title of one note (loaded before the call, since the annotation names the entity
    type)."""
    return note.title


@workflow(name="note_titles")
async def note_titles(
    context: WorkflowContext, notes: Annotated[ArrayHandle[BoundID[Note]], "[topic: str]"]
) -> Annotated[ArrayHandle[str], "[topic: str]"]:
    """The title of every note in ``notes``: the step, mapped over the topics."""
    return context.run_step("title", note_title, {"note": notes}, map_over=["topic"]).expect_type(
        ("[topic: str]", str)
    )


async def walkthrough(client: ProjectClient) -> None:
    """Walk through the project array API on ``client``, printing as it goes. The client must be
    able to run :func:`note_titles`."""
    # 1. Declare the array. Doing it again with the same type is a no-op.
    info = await client.create_array("notes", NOTES_TYPE)
    print(f"declared {info.name!r}: {format_type(info.type)}")

    # 2. Store entities and upsert an array referencing them. The entities are persisted with the
    #    rows that reference them.
    notes = {
        "postgres": Note(title="Postgres", body="Keeps the experiment's state."),
        "arrays": Note(title="Arrays", body="Sparse named arrays with one value schema."),
    }
    ids = {topic: (await client.store(note)).entity_id for topic, note in notes.items()}
    info = await client.upsert_rows(
        "notes", Array.from_items([((t,), i) for t, i in ids.items()], NOTES_TYPE)
    )
    print(f"upserted: {info.row_count} rows")

    # 3. Load the array back and follow a reference.
    array = await client.load_array("notes")
    print()
    print(format_array(array))
    note = await client.load(array[("arrays",)], as_type=Note)
    print(f"\nnotes[arrays] -> {note.title!r}: {note.body}")

    # 4. Snapshot: the rows as they are now, as an Array entity.
    first = await client.snapshot_array("notes")
    print(f"\nsnapshot: {first.array.entity_id!r}")
    again = await client.snapshot_array("notes")
    print(f"snapshot again, nothing changed: same snapshot -> {again == first}")

    # 5. Edit, snapshot again, and compare.
    extra = await client.store(Note(title="Snapshots", body="Rows as they were, as an entity."))
    await client.upsert_rows(
        "notes", Array.from_items([(("snapshots",), extra.entity_id)], NOTES_TYPE)
    )
    second = await client.snapshot_array("notes")
    print(f"after an upsert: a new snapshot, new entity -> {second.array != first.array}")
    await client.delete_rows("notes", [("snapshots",)])
    third = await client.snapshot_array("notes")
    print(
        "after deleting it again: a new snapshot, but the same entity as the first"
        f" -> {third.array == first.array}"
    )
    print("\nsnapshots, oldest first:")
    for snap in await client.list_snapshots("notes"):
        print(f"  {snap.created_at:%H:%M:%S.%f}: {snap.array.entity_id!r}")
    old = await client.load(second.array)
    print(f"\nthe second snapshot still loads, with {len(old)} rows:")
    print(format_array(old))

    # 6. Pass a snapshot to a job. The snapshot's bound id is the job's input: the root
    #    invocation's input node references the snapshot entity itself, so the job record says
    #    exactly which rows it ran on.
    handle = await client.submit(note_titles, {"notes": third.array})
    status = await handle.wait()
    print(f"\njob {status.job_id}: {status.state}")
    print(format_array(await handle.result()))
    # What the launch recorded, read back through the project's entity store: the root's input
    # node references the snapshot entity itself.
    root = await client.jobs.describe_invocation(status.job_id, "root")  # type: ignore[attr-defined]
    assert root is not None
    (given,) = [n.node for n in root.description.nodes if isinstance(n.node, nodes.Input)]
    assert isinstance(given, nodes.Input)
    print(f"root input {given.name!r} is the snapshot -> {given.array == third.array}")
