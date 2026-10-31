"""Helpers shared by the Postgres runner's tests: staging a job with a job attempt that holds a
lease, starting its root, expanding a run node into children, making step requests, aging a lease,
checking the status relations against the rules the write paths act on, running two transactions
against each other, running a :class:`JobRunner` over a job (in the current task, or in a second
one the test acts against), and a :class:`LocalProjectClient` over a fake environment that pins a
fixture code version without touching any checkout."""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
import pytest
from frozendict import frozendict
from sqlalchemy import func, select

from fxtr.core.entities import BoundID
from fxtr.entity_defns.array import Array, ArrayType
from fxtr.experiment.registry import FunctionRegistry
from fxtr.project import config
from fxtr.project.code_version import CodeVersion
from fxtr.project.config import Project
from fxtr.project.environment import Environment, Pin, Problem, pin_ref
from fxtr.runner import nodes
from fxtr.runner.driver import in_process_executor
from fxtr.runner.session import AttemptID

pytest.importorskip("sqlalchemy")

from fxtr.runner.postgres import execution, ledger  # noqa: E402
from fxtr.runner.postgres.client import LocalProjectClient  # noqa: E402
from fxtr.runner.postgres.database import Database, Transaction  # noqa: E402
from fxtr.runner.postgres.entities import PostgresEntityStorage  # noqa: E402
from fxtr.runner.postgres.execution import (  # noqa: E402
    CacheEntryRecord,
    JobAttemptID,
    JobAttemptRecord,
    Started,
    StartedStep,
    StartInvocation,
    StartStep,
    StepResolution,
)
from fxtr.runner.postgres.ledger import (  # noqa: E402
    ChildSpec,
    Emission,
    InvocationRecord,
    JobID,
    StepResultID,
)
from fxtr.runner.postgres.runner import (  # noqa: E402
    JobRunner,
    RunLimits,
    RunnerSettings,
)
from fxtr.runner.postgres.schema import attempts, job_attempts  # noqa: E402
from fxtr.runner.postgres.status_queries import StatusQueries  # noqa: E402
from fxtr.runner.steps import StepRequest  # noqa: E402

OWNER = "test-owner"
"""The owner string the staged job attempts hold their lease under."""

FAST = RunnerSettings(heartbeat_interval=0.05, reconcile_interval=0.2, poll_interval=0.05)
"""Short clocks, so a test sees several heartbeats and reconciliation passes in a moment."""


TEST_CODE_VERSION = CodeVersion(
    commit="0" * 40,
    tree="0" * 40,
    ephemeral=False,
    project_path=".",
    environment_verified=True,
    functions=frozendict(),
)
"""The code version a process in these tests claims to run when it takes a job directly: a
fixture, since nothing here pins a checkout. (A client builds its own, from the pin
:class:`FakeEnvironment` gives it.)"""


async def a_code_version(database: Database) -> BoundID[CodeVersion]:
    """:data:`TEST_CODE_VERSION`, committed to the entity store, so any later transaction may
    reference it."""
    entities = PostgresEntityStorage(database)
    code_version = await entities.store(TEST_CODE_VERSION)
    async with database.transaction() as tx:
        await entities.flush(tx)
    return code_version


def produced_by(attempt_id: AttemptID) -> StepResultID:
    """The step result a step attempt produced: it has the attempt's id."""
    return ledger.step_result_of(attempt_id)


def behind(entry: CacheEntryRecord) -> AttemptID:
    """The attempt behind a cache entry: its current one, or the one that produced the step result
    it offers."""
    if entry.attempt_id is not None:
        return entry.attempt_id
    assert entry.step_result_id is not None
    return ledger.producing_attempt(entry.step_result_id)


async def a_job(
    database: Database,
    root: str = "root",
    *,
    owner: str = OWNER,
    overwrite_cache_conflicts: bool = False,
) -> JobID:
    """A job of workflow ``wf`` with its first job attempt, owned by ``owner`` with a fresh
    lease, which overwrites cache conflicts if ``overwrite_cache_conflicts``."""
    job_id = JobID(uuid.uuid4())
    code_version = await a_code_version(database)
    async with database.transaction() as tx:
        await ledger.create_job(tx, job_id, "wf", root, inputs={})
        await execution.create_job_attempt(
            tx,
            job_id,
            owner=owner,
            code_version=code_version,
            overwrite_cache_conflicts=overwrite_cache_conflicts,
        )
    return job_id


async def active(database: Database, job_id: JobID) -> JobAttemptRecord:
    """The job's active job attempt, which must exist."""
    async with database.transaction() as tx:
        job_attempt = await execution.active_job_attempt(tx, job_id)
    assert job_attempt is not None
    return job_attempt


async def pending(database: Database, job_id: JobID) -> list[Any]:
    """What the job's active job attempt is due to start."""
    await assert_policy_agrees(database, job_id)
    job_attempt = await active(database, job_id)
    async with database.transaction() as tx:
        return await execution.pending_work(tx, job_attempt)


async def start_root(database: Database, job_id: JobID) -> Started:
    """Start the job's root invocation under its active job attempt, which must be due."""
    job_attempt = await active(database, job_id)
    async with database.transaction() as tx:
        root = await ledger.find_invocation(tx, job_id, "root")
        started = await execution.start_attempt(tx, job_attempt, StartInvocation(root.id))
    assert isinstance(started, Started)
    return started


async def start_invocation(
    database: Database, job_id: JobID, invocation: InvocationRecord
) -> Started:
    """Start an invocation under the job's active job attempt, which must be due."""
    job_attempt = await active(database, job_id)
    async with database.transaction() as tx:
        started = await execution.start_attempt(tx, job_attempt, StartInvocation(invocation.id))
    assert isinstance(started, Started)
    return started


async def expand_children(
    database: Database, started: Started
) -> tuple[nodes.RunChildWorkflow, list[InvocationRecord]]:
    """Have the started root attempt emit a source array and a run node mapped over ``k`` with two
    keys, and expand it; returns the run node and the two children (created, not started)."""
    entities = PostgresEntityStorage(database)
    x = Array.from_items([((0,), 1), ((1,), 2)], ("[k: int]", int))
    x_ref = await entities.store(x)
    slices = {k: await entities.store(Array.scalar(v, int)) for k, v in ((0, 1), (1, 2))}
    source = nodes.SourceArray(name="x", array=x_ref)
    run = nodes.RunChildWorkflow(
        name="child",
        workflow_name="child_wf",
        inputs=frozendict({"x": source.content_id()}),
        map_over=("k",),
        join="exact",
        replicas=frozendict(),
        cache_key_prefix="root/child",
        type=ArrayType.of(("[k: int]", int)),
    )
    children = [
        ChildSpec(
            key=(k,),
            pathname=f"root/child[k:{k}]",
            workflow_name="child_wf",
            cache_key_prefix=f"root/child[k:{k}]",
            inputs={"x": (slices[k], ArrayType.of(int))},
        )
        for k in (0, 1)
    ]
    async with database.transaction() as tx:
        act = await execution.guard(tx, started.attempt_id)
        await entities.flush(tx)
        await ledger.record_emissions(tx, act, [Emission(source, x.type), Emission(run, run.type)])
        created = await ledger.expand_run_node(tx, act, run.content_id(), children)
    return run, created


async def a_request(database: Database, value: int) -> Any:
    """A stored step request for step ``s`` on one scalar input; ``value`` varies the
    fingerprint."""
    entities = PostgresEntityStorage(database)
    x = await entities.store(Array.scalar(value, int))
    request = await entities.store(StepRequest(step_name="s", inputs=frozendict({"x": x})))
    async with database.transaction() as tx:
        await entities.flush(tx)
    return request


async def running_root(
    database: Database, *, owner: str = OWNER, overwrite_cache_conflicts: bool = False
) -> tuple[JobID, AttemptID]:
    """A job whose root attempt is running under its job attempt: something that can make step
    requests. Its job attempt overwrites cache conflicts if ``overwrite_cache_conflicts``."""
    job_id = await a_job(database, owner=owner, overwrite_cache_conflicts=overwrite_cache_conflicts)
    started = await start_root(database, job_id)
    return job_id, started.attempt_id


async def request(
    database: Database, job_id: JobID, attempt_id: AttemptID, key: str, req: Any
) -> StepResolution:
    """Record the running attempt's job's request at a key and say how it resolves, in one
    transaction."""
    async with database.transaction() as tx:
        act = await execution.guard(tx, attempt_id)
        recorded = await ledger.record_step_request(tx, act, key, req, "s")
        resolution = await execution.resolve_step(tx, job_id, key, recorded.request)
    await assert_policy_agrees(database, job_id)
    return resolution


async def take(database: Database, attempt_id: AttemptID, key: str) -> Any:
    """Have the running attempt's job take the result an equal completed attempt offers at the
    key (writing the job's binding), the way its evaluator would; returns the result, or ``None``
    if nothing is on offer."""
    async with database.transaction() as tx:
        act = await execution.guard(tx, attempt_id)
        taken = await execution.take_step_result(tx, act, key)
    await assert_policy_agrees(database, act.job_id)
    return taken


async def settle(database: Database, job_id: JobID) -> JobAttemptRecord | None:
    """Settle the job's active job attempt if its work is finished or exhausted, the way its
    runner does when nothing it runs is live: in a transaction of its own."""
    async with database.transaction() as tx:
        settled = (await execution.settle(tx, job_id)).settled
    await assert_policy_agrees(database, job_id)
    return settled


async def start_step(database: Database, job_id: JobID, key: str) -> StartedStep | None:
    """Start the job's step request at a key under its active job attempt, if it resolves to
    running it."""
    job_attempt = await active(database, job_id)
    async with database.transaction() as tx:
        started = await execution.start_attempt(tx, job_attempt, StartStep(key))
    assert started is None or isinstance(started, StartedStep)
    await assert_policy_agrees(database, job_id)
    return started


async def age_heartbeat(database: Database, job_attempt_id: JobAttemptID, seconds: float) -> None:
    """Stage a stale lease: move the job attempt's heartbeat ``seconds`` into the past."""
    async with database.transaction() as tx:
        await tx.execute(
            job_attempts.update()
            .where(job_attempts.c.id == job_attempt_id)
            .values(heartbeat_at=func.now() - timedelta(seconds=seconds))
        )


async def set_attempt_state(database: Database, attempt_id: AttemptID, state: str) -> None:
    """Stage an attempt's state directly, bypassing the close hook."""
    async with database.transaction() as tx:
        await tx.execute(
            attempts.update().where(attempts.c.id == uuid.UUID(attempt_id)).values(state=state)
        )
        job_id = (
            await tx.execute(
                select(job_attempts.c.job_id)
                .join(attempts, attempts.c.owner_job_attempt_id == job_attempts.c.id)
                .where(attempts.c.id == uuid.UUID(attempt_id))
            )
        ).scalar_one()
    await assert_policy_agrees(database, JobID(job_id))


async def set_job_attempt_state(database: Database, job_id: JobID, state: str) -> None:
    """Stage the job's active job attempt's state directly."""
    async with database.transaction() as tx:
        await tx.execute(
            job_attempts.update()
            .where(job_attempts.c.job_id == job_id, job_attempts.c.state == "active")
            .values(state=state)
        )
    await assert_policy_agrees(database, job_id)


# ---- the status relations against the write paths ----

# Every judgement of the step relation (see ``status_queries``' docstring), with the cache
# resolutions ``resolve_step`` may give alongside it: ``failed`` and ``stopped`` overlay any
# resolution but a binding.
_RESOLUTIONS_OF: dict[str, tuple[type, ...]] = {
    "done": (execution.Bound,),
    "ready": (execution.Completed,),
    "conflict": (execution.ConflictingEntry,),
    "running": (execution.Pending,),
    "queued": (execution.RunIt,),
    "failed": (execution.Completed, execution.ConflictingEntry, execution.Pending, execution.RunIt),
    "stopped": (
        execution.Completed,
        execution.ConflictingEntry,
        execution.Pending,
        execution.RunIt,
    ),
}


async def assert_step_policy_agrees(tx: Transaction, job_id: JobID, key: str) -> None:
    """Check the step relation's judgement of the job's request at a key against the Python
    statement of the cache rules the write paths act on. A disagreement leaves a run node waiting
    on what nothing will do: a result shown ready that ``take_step_result`` will not bind (it
    resolves again), or a step judged due that ``start_attempt`` refuses to start.

    ``pending_work`` lists the relation's ``queued`` rows, so what is due is checked against the
    rule ``start_attempt`` applies: the key resolves to ``RunIt`` under an active job attempt that
    has not already failed there."""
    request = await ledger.get_job_step_request(tx, job_id, key)
    record = (await StatusQueries([job_id]).step_records(tx, [key])).get(key)
    if request is None:
        assert record is None
        return
    assert record is not None
    resolution = await execution.resolve_step(tx, job_id, key, request.request)
    assert isinstance(resolution, _RESOLUTIONS_OF[record.target_state]), (record, resolution)
    if record.target_state == "ready":
        assert isinstance(resolution, execution.Completed)
        assert record.offered_step_result_id == resolution.step_result_id
    if record.target_state == "running":
        assert isinstance(resolution, execution.Pending)
        assert record.current_attempt_id == resolution.attempt_id
    job_attempt = await execution.active_job_attempt(tx, job_id)
    failed_here = job_attempt is not None and any(
        a.cache_key == key and a.state == "failed"
        for a in await execution.list_owned_attempts(tx, job_attempt.id)
    )
    due = job_attempt is not None and isinstance(resolution, execution.RunIt) and not failed_here
    assert due == (record.target_state == "queued"), (record, resolution)
    listed = job_attempt is not None and StartStep(key) in await execution.pending_work(
        tx, job_attempt
    )
    assert listed == due, (record, resolution)


async def assert_policy_agrees(database: Database, job_id: JobID) -> None:
    """Check every step the job requested and every invocation of it, in one snapshot. The
    helpers here that change or read a job's execution state call it, so every scenario built from
    them checks agreement at each step it takes."""
    async with database.transaction(snapshot=True) as tx:
        for recorded in await ledger.list_job_step_requests(tx, job_id):
            await assert_step_policy_agrees(tx, job_id, recorded.cache_key)
        for invocation in await ledger.list_invocations(tx, job_id):
            await assert_invocation_display_agrees(tx, invocation)


async def assert_invocation_display_agrees(tx: Transaction, invocation: InvocationRecord) -> None:
    """Check that an invocation is shown ``queued`` exactly when something is about to start it:
    it is due under the active job attempt (unfinished, not running, and every attempt the job
    attempt made at it abandoned); and that ``pending_work`` lists it exactly then."""
    state, _ = await execution.invocation_state(tx, invocation)
    job_attempt = await execution.active_job_attempt(tx, invocation.job_id)
    if job_attempt is None:
        assert state != "queued", state
        return
    tried = await execution.list_attempts(tx, invocation.id)
    due = (
        not invocation.finished
        and not any(a.live for a in tried)
        and all(a.state == "abandoned" for a in tried if a.owner_job_attempt_id == job_attempt.id)
    )
    assert due == (state == "queued"), state
    listed = StartInvocation(invocation.id) in await execution.pending_work(tx, job_attempt)
    assert listed == due, state


# ---- running a job ----


async def a_runner(
    database: Database,
    job_id: JobID,
    *functions: Any,
    owner: str = "runner",
    limits: RunLimits = RunLimits(),
    settings: RunnerSettings = FAST,
    verify_determinism: bool = False,
    overwrite_cache_conflicts: bool = False,
) -> JobRunner:
    """A runner holding the job's lease, over the in-process executor for ``functions``.
    Determinism verification is off by default, so a gated body runs once and a test's gate is
    unambiguous. ``overwrite_cache_conflicts`` is recorded on the job attempt it takes."""
    code_version = await a_code_version(database)
    async with database.transaction() as tx:
        job_attempt = await execution.resume(
            tx,
            job_id,
            owner=owner,
            code_version=code_version,
            overwrite_cache_conflicts=overwrite_cache_conflicts,
        )
    assert isinstance(job_attempt, JobAttemptRecord)
    return JobRunner(
        database,
        job_attempt,
        execute=in_process_executor(FunctionRegistry(functions)),
        limits=limits,
        settings=settings,
        verify_determinism=verify_determinism,
    )


@dataclasses.dataclass
class Run:
    """What a runner running in its own task ended with.

    Attributes:
        settled: The job attempt it returned, once it has returned.
        error: What it raised instead, if it raised.
    """

    settled: JobAttemptRecord | None = None
    error: Exception | None = None
    _scope: anyio.CancelScope = dataclasses.field(default_factory=anyio.CancelScope)

    def kill(self) -> None:
        """Stop the runner the abrupt way: cancel its task, as a process being killed would. Its
        attempts are abandoned and its job attempt settled ``cancelled`` by the shielded teardown,
        so the block ends once that has landed."""
        self._scope.cancel()


@asynccontextmanager
async def runner_task(runner: JobRunner) -> AsyncIterator[Run]:
    """Run ``runner`` in a task while the block acts on the database from outside. The block ends
    when the runner does, and the yielded record then says how."""
    outcome = Run()

    async def run() -> None:
        with outcome._scope:
            try:
                outcome.settled = await runner.run()
            except Exception as exc:
                outcome.error = exc

    async with anyio.create_task_group() as tg:
        tg.start_soon(run)
        yield outcome


async def wait_until(condition: Callable[[], Awaitable[bool]], *, timeout: float = 5) -> None:
    """Poll until the condition holds, or fail the test."""
    with anyio.fail_after(timeout):
        while not await condition():
            await anyio.sleep(0.02)


async def run_blocked(
    work: Callable[[], Awaitable[Any]], release: Callable[[], Awaitable[None]]
) -> Any:
    """Start ``work`` in a task, check it has not finished after a moment (it is blocked on a
    lock ``release`` holds), then run ``release`` and return what ``work`` returned."""
    done = anyio.Event()
    result: list[Any] = []

    async def run() -> None:
        result.append(await work())
        done.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(run)
        await anyio.sleep(0.2)
        assert not done.is_set(), "the work was not blocked"
        await release()
        with anyio.fail_after(5):
            await done.wait()
    return result[0]


__all__ = [
    "FAST",
    "OWNER",
    "Run",
    "TEST_CODE_VERSION",
    "a_job",
    "a_code_version",
    "a_request",
    "a_runner",
    "behind",
    "produced_by",
    "active",
    "age_heartbeat",
    "expand_children",
    "pending",
    "request",
    "run_blocked",
    "runner_task",
    "running_root",
    "set_attempt_state",
    "set_job_attempt_state",
    "settle",
    "start_invocation",
    "start_root",
    "start_step",
    "take",
    "wait_until",
]


# ---- the client over a fake environment ----


@dataclasses.dataclass
class FakeEnvironment(Environment):
    """An :class:`~fxtr.project.environment.Environment` with canned answers: it pins ``commit``
    (:data:`TEST_CODE_VERSION`'s by default) for any project without running a tool, with the
    project's root as the repository's, reports the problems it was given, and records what it was
    asked. Projects are located the real way, by reading files.

    Attributes:
        commit: The commit ``pin_code`` pins.
        tree: Its tree.
        ephemeral: Whether the pin is ephemeral.
        known_commits: The commits the repository contains, for ``missing_commits``; ``None``
            for every commit.
        problems: What ``check_synced`` reports.
        committed_files: What ``committed_file`` reads, by commit and path; any other file is
            missing.
        pins: Each ``pin_code`` call's ``allow_staged``, in order.
        checks: How many times ``check_synced`` was called.
    """

    commit: str = TEST_CODE_VERSION.commit
    tree: str = TEST_CODE_VERSION.tree
    ephemeral: bool = False
    known_commits: frozenset[str] | None = None
    problems: Sequence[Problem] = ()
    committed_files: Mapping[tuple[str, str], bytes] = dataclasses.field(default_factory=dict)
    pins: list[bool] = dataclasses.field(default_factory=list)
    checks: int = 0

    @property
    def cwd(self) -> Path:
        return Path.cwd()

    def locate_project(self, anchor: Path | None = None) -> Project:
        return config.locate_project(self.cwd if anchor is None else anchor)

    async def pin_code(self, project: Project, *, allow_staged: bool) -> Pin:
        self.pins.append(allow_staged)
        return Pin(
            commit=self.commit,
            tree=self.tree,
            ephemeral=self.ephemeral,
            ref=pin_ref(self.commit),
            repo_root=project.root,
            project_path=".",
        )

    async def missing_commits(self, project: Project, commits: Iterable[str]) -> list[str]:
        known = self.known_commits
        return [] if known is None else [c for c in commits if c not in known]

    async def committed_file(self, project: Project, commit: str, path: str) -> bytes | None:
        return self.committed_files.get((commit, path))

    async def check_synced(self, project: Project) -> list[Problem]:
        self.checks += 1
        return list(self.problems)


STALE_AFTER = timedelta(seconds=0.5)
"""The staleness threshold the test projects declare: long enough for the fast heartbeat to keep
a lease fresh, short enough for a test to age one."""


def a_project(database: Database, *, stale_after: timedelta = STALE_AFTER) -> Project:
    """A project whose database is ``database``, declaring no modules."""
    assert database.pg_schema is not None
    return Project(
        root=Path.cwd(),
        name="test",
        modules=(),
        stale_after=stale_after,
        database_url=database.url,
        schema=database.pg_schema,
    )


def a_client(
    database: Database,
    *functions: Any,
    environment: Environment | None = None,
    settings: RunnerSettings = FAST,
    verify_determinism: bool = False,
    verify_environment: bool = True,
    limits: RunLimits = RunLimits(),
) -> LocalProjectClient:
    """A local client over ``database`` whose registry holds exactly ``functions``, with a fake
    environment, fast clocks, no determinism verification (as for :func:`a_runner`), and no
    Ctrl-C handling. The client does not own the database's schema: the fixture does."""
    return LocalProjectClient(
        database,
        project=a_project(database),
        environment=FakeEnvironment() if environment is None else environment,
        registry=FunctionRegistry(functions),
        limits=limits,
        settings=settings,
        verify_determinism=verify_determinism,
        verify_environment=verify_environment,
        handle_sigint=False,
    )
