"""Step previews on the Postgres runner: ``ctx.preview`` written by the step attempt's flush task,
throttled to the latest, visible while the step runs and read back as bytes, kept after it fails,
and deleted when it completes.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import anyio
import pytest
from postgres_testing import a_client, a_runner, runner_task, wait_until

from fxtr.core import codec
from fxtr.core.entity_data import get_entity_type_tag
from fxtr.entity_defns.array import Array
from fxtr.entity_defns.converter import DEFAULT_CONVERTER
from fxtr.entity_defns.dataclass_bases import TaggedDataclass
from fxtr.entity_defns.partial import Partial
from fxtr.experiment.handles import ArrayHandle
from fxtr.experiment.steps import StepContext, step
from fxtr.experiment.workflows import WorkflowContext, workflow

pytest.importorskip("sqlalchemy")

from sqlalchemy import event  # noqa: E402

from fxtr.runner.postgres.client import LocalProjectClient  # noqa: E402
from fxtr.runner.postgres.database import Database  # noqa: E402
from fxtr.runner.postgres.ledger import JobID  # noqa: E402

pytestmark = pytest.mark.anyio

KEY = "root/draft"


@dataclasses.dataclass(frozen=True, kw_only=True)
class Draft(TaggedDataclass, fxtr_type="test.fxtr.previews.Draft"):
    text: str
    words: int


@dataclasses.dataclass(frozen=True, kw_only=True)
class Untagged(TaggedDataclass):
    """A tagged dataclass that declares no type, so it cannot be serialized."""

    text: str


GATE: anyio.Event | None = None


@pytest.fixture
def client(database: Database) -> LocalProjectClient:
    return a_client(database)


@pytest.fixture(autouse=True)
def reset_gate() -> None:
    global GATE
    GATE = None


def decode(data: bytes) -> Partial[Draft] | Draft:
    raw = codec.decode(data)
    if get_entity_type_tag(raw) == Draft.fxtr_type_id():
        return DEFAULT_CONVERTER.structure(raw, Draft)
    return DEFAULT_CONVERTER.structure(raw, Partial[Draft])


async def run(client: LocalProjectClient, job: JobID, *functions: Any) -> str:
    runner = await a_runner(client.database, job, *functions)
    await runner.run()
    return (await client.jobs.status(job)).state


@step(inputs={"n": int}, output=str)
async def streaming_step(context: StepContext, n: int) -> str:
    text = ""
    for i in range(n):
        text += f"w{i} "
        context.preview(Partial(Draft, text=text))
    if GATE is not None:
        await GATE.wait()
    context.preview(Draft(text=text, words=n))
    return text


@workflow(inputs={"n": int}, output=str)
async def streaming_root(context: WorkflowContext, n: ArrayHandle[int]) -> ArrayHandle[str]:
    return context.run_step("draft", streaming_step, {"n": n})


async def test_a_completed_step_keeps_no_preview(client: LocalProjectClient) -> None:
    job = await client.jobs.create(streaming_root, {"n": Array.scalar(50, int)})
    assert await run(client, job, streaming_root, streaming_step) == "succeeded"
    described = (await client.jobs.describe(job)).steps[KEY]
    assert described.attempt is not None and described.preview is None
    assert await client.jobs.get_preview(described.attempt.id) is None


async def test_a_preview_is_visible_while_the_step_runs_and_deleted_when_it_completes(
    client: LocalProjectClient,
) -> None:
    global GATE
    GATE = anyio.Event()
    job = await client.jobs.create(streaming_root, {"n": Array.scalar(3, int)})
    runner = await a_runner(client.database, job, streaming_root, streaming_step)
    async with runner_task(runner):

        async def previewed() -> bool:
            detail = await client.jobs.describe_step(job, KEY)
            return detail is not None and detail.description.preview is not None

        await wait_until(previewed)
        detail = await client.jobs.describe_step(job, KEY)
        assert detail is not None and detail.description.attempt is not None
        attempt = detail.description.attempt
        assert attempt.state == "running"
        found = await client.jobs.get_preview(attempt.id)
        assert found is not None
        assert found[0] == detail.description.preview
        # The burst of three coalesced into its last before the step got to its gate.
        assert found[0].seq == 3
        assert decode(found[1]) == Partial(Draft, text="w0 w1 w2 ")
        GATE.set()
    assert (await client.jobs.status(job)).state == "succeeded"
    # The result took the preview's place: the row written while the step ran is gone.
    described = (await client.jobs.describe(job)).steps[KEY]
    assert described.attempt is not None and described.attempt.id == attempt.id
    assert described.preview is None
    assert await client.jobs.get_preview(attempt.id) is None


@step(inputs={"n": int}, output=str)
async def failing_step(context: StepContext, n: int) -> str:
    context.preview(Partial(Draft, text="partial"))
    raise RuntimeError("boom")


@workflow(inputs={"n": int}, output=str)
async def failing_root(context: WorkflowContext, n: ArrayHandle[int]) -> ArrayHandle[str]:
    return context.run_step("draft", failing_step, {"n": n})


async def test_a_failed_step_keeps_its_last_preview(client: LocalProjectClient) -> None:
    job = await client.jobs.create(failing_root, {"n": Array.scalar(1, int)})
    assert await run(client, job, failing_root, failing_step) == "stopped"
    described = (await client.jobs.describe(job)).steps[KEY]
    assert described.attempt is not None and described.attempt.state == "failed"
    found = await client.jobs.get_preview(described.attempt.id)
    assert found is not None and decode(found[1]) == Partial(Draft, text="partial")


@step(inputs={"n": int}, output=str)
async def unserializable_preview_step(context: StepContext, n: int) -> str:
    context.preview(Untagged(text="no type"))
    context.log("still running")
    await anyio.sleep(0.05)  # the flush task tries the preview: a completing step's last is not
    return "done"


@workflow(inputs={"n": int}, output=str)
async def unserializable_root(context: WorkflowContext, n: ArrayHandle[int]) -> ArrayHandle[str]:
    return context.run_step("draft", unserializable_preview_step, {"n": n})


async def test_a_preview_that_cannot_be_serialized_is_dropped(
    client: LocalProjectClient, caplog: pytest.LogCaptureFixture
) -> None:
    job = await client.jobs.create(unserializable_root, {"n": Array.scalar(1, int)})
    assert await run(client, job, unserializable_root, unserializable_preview_step) == "succeeded"
    described = (await client.jobs.describe(job)).steps[KEY]
    assert described.preview is None
    assert [log.message for log in described.logs] == ["still running"]
    assert "could not be built or serialized" in caplog.text


@step(inputs={"n": int}, output=int)
async def plain_step(context: StepContext, n: int) -> int:
    return n


@workflow(inputs={"n": int}, output=int)
async def plain_root(context: WorkflowContext, n: ArrayHandle[int]) -> ArrayHandle[int]:
    return context.run_step("draft", plain_step, {"n": n})


async def test_a_step_without_previews_has_none(client: LocalProjectClient) -> None:
    job = await client.jobs.create(plain_root, {"n": Array.scalar(1, int)})
    assert await run(client, job, plain_root, plain_step) == "succeeded"
    described = (await client.jobs.describe(job)).steps[KEY]
    assert described.attempt is not None and described.preview is None
    assert await client.jobs.get_preview(described.attempt.id) is None


@step(inputs={"n": int}, output=int)
async def trickling_step(context: StepContext, n: int) -> int:
    for i in range(n):
        context.preview(Partial(Draft, words=i + 1))
        await anyio.sleep(0.01)
    raise RuntimeError("boom")  # a failed step keeps its last preview, for the test to read


@workflow(inputs={"n": int}, output=int)
async def trickling_root(context: WorkflowContext, n: ArrayHandle[int]) -> ArrayHandle[int]:
    return context.run_step("draft", trickling_step, {"n": n})


async def test_previews_are_written_at_most_once_per_interval(
    client: LocalProjectClient, database: Database
) -> None:
    writes: list[str] = []

    def count(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        if statement.lstrip().upper().startswith("INSERT INTO") and "attempt_previews" in statement:
            writes.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", count)
    try:
        job = await client.jobs.create(trickling_root, {"n": Array.scalar(60, int)})
        assert await run(client, job, trickling_root, trickling_step) == "stopped"
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", count)
    described = (await client.jobs.describe(job)).steps[KEY]
    assert described.preview is not None and described.preview.seq == 60
    # About 0.6s of previews every 10ms: one write per 0.25s interval, plus the last.
    assert 2 <= len(writes) <= 6, len(writes)
    assert described.attempt is not None
    found = await client.jobs.get_preview(described.attempt.id)
    assert found is not None and decode(found[1]) == Partial(Draft, words=60)


@step(inputs={"fail": bool}, output=str)
async def ending_mid_write_step(context: StepContext, fail: bool) -> str:
    context.preview(Partial(Draft, text="first"))
    await anyio.sleep(0.1)  # the flush task is writing "first" now, slowly
    context.preview(Partial(Draft, text="last"))
    context.log("last line")
    if fail:
        raise RuntimeError("boom")
    return "done"


@workflow(inputs={"fail": bool}, output=str)
async def ending_mid_write_root(
    context: WorkflowContext, fail: ArrayHandle[bool]
) -> ArrayHandle[str]:
    return context.run_step("draft", ending_mid_write_step, {"fail": fail})


@pytest.mark.parametrize(("fail", "state"), [(False, "succeeded"), (True, "stopped")])
async def test_what_a_step_publishes_during_a_slow_write_is_written_when_it_ends(
    client: LocalProjectClient, monkeypatch: pytest.MonkeyPatch, fail: bool, state: str
) -> None:
    """A step that ends (completing or failing) while its flush task is still writing an earlier
    preview keeps its last log line, and a failing one its last preview: the task makes one more
    pass for them. A completing one writes no last preview, and the one it wrote is deleted."""
    from fxtr.runner.postgres import attempts

    record_preview = attempts.record_preview
    written: list[int] = []

    async def slow_record_preview(tx: Any, attempt_id: Any, seq: int, data: bytes) -> None:
        await anyio.sleep(0.5)
        await record_preview(tx, attempt_id, seq, data)
        written.append(seq)

    monkeypatch.setattr(attempts, "record_preview", slow_record_preview)
    job = await client.jobs.create(ending_mid_write_root, {"fail": Array.scalar(fail, bool)})
    assert await run(client, job, ending_mid_write_root, ending_mid_write_step) == state
    described = (await client.jobs.describe(job)).steps[KEY]
    assert described.attempt is not None
    assert [log.message for log in described.logs] == ["last line"]
    found = await client.jobs.get_preview(described.attempt.id)
    if not fail:
        assert written == [1]
        assert described.preview is None and found is None
        return
    assert written == [1, 2]
    assert found is not None
    assert found[0].seq == 2
    assert decode(found[1]) == Partial(Draft, text="last")


BUILDS: list[int] = []


@step(inputs={"n": int}, output=int)
async def lazy_step(context: StepContext, n: int) -> int:
    words = 0

    def build() -> Partial[Draft]:
        BUILDS.append(words)
        return Partial(Draft, words=words)

    for _ in range(n):
        words += 1
        context.preview(build)
        await anyio.sleep(0.005)
    raise RuntimeError("boom")  # a failed step keeps its last preview, for the test to read


@workflow(inputs={"n": int}, output=int)
async def lazy_root(context: WorkflowContext, n: ArrayHandle[int]) -> ArrayHandle[int]:
    return context.run_step("draft", lazy_step, {"n": n})


async def test_a_preview_function_is_called_only_when_the_preview_is_written(
    client: LocalProjectClient,
) -> None:
    """A step publishing a function on every event builds the preview once per write, not once per
    event, and the last write (after the body raised) sees the body's final state."""
    BUILDS.clear()
    job = await client.jobs.create(lazy_root, {"n": Array.scalar(100, int)})
    assert await run(client, job, lazy_root, lazy_step) == "stopped"
    described = (await client.jobs.describe(job)).steps[KEY]
    assert described.preview is not None and described.preview.seq == 100
    assert described.attempt is not None
    found = await client.jobs.get_preview(described.attempt.id)
    assert found is not None and decode(found[1]) == Partial(Draft, words=100)
    # About 0.5s of events, one every 5ms: a build per 0.25s interval, plus the last.
    assert 2 <= len(BUILDS) <= 6, BUILDS
    assert BUILDS[-1] == 100


@step(inputs={"broken": str}, output=str)
async def broken_preview_step(context: StepContext, broken: str) -> str:
    def build() -> Any:
        if broken == "raises":
            raise ValueError("no preview today")
        return {"text": "a dict"}

    context.preview(build)
    context.log("still running")
    await anyio.sleep(0.05)  # the flush task tries the preview: a completing step's last is not
    return "done"


@workflow(inputs={"broken": str}, output=str)
async def broken_preview_root(
    context: WorkflowContext, broken: ArrayHandle[str]
) -> ArrayHandle[str]:
    return context.run_step("draft", broken_preview_step, {"broken": broken})


@pytest.mark.parametrize("broken", ["raises", "returns a dict"])
async def test_a_preview_function_that_fails_drops_its_preview(
    client: LocalProjectClient, caplog: pytest.LogCaptureFixture, broken: str
) -> None:
    job = await client.jobs.create(broken_preview_root, {"broken": Array.scalar(broken, str)})
    assert await run(client, job, broken_preview_root, broken_preview_step) == "succeeded"
    described = (await client.jobs.describe(job)).steps[KEY]
    assert described.preview is None
    assert [log.message for log in described.logs] == ["still running"]
    assert "could not be built or serialized" in caplog.text
