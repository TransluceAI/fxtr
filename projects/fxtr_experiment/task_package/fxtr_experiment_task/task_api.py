"""Task-facing contracts: what task and step code is written against.

Plain Python protocols over the view calculus (`views.py`). Two levels:

- The **outer level** (:class:`TaskFn` running against :class:`TaskContext`)
  may only perform operations that are idempotent, deterministic, and
  trackable: view rewrites (free), reads/writes/demands, opening subgrids,
  and launching checkpointed steps. Determinism is *strongly* enforced by the
  safety replay check (set-based, keyed by path/coordinates — ordinary anyio
  concurrency is fine); helper base classes catching footguns early are
  best-effort UX on top.
- The **inner level** (:class:`StepFn` running against :class:`StepContext`)
  runs durably into a checkpoint cell and is where all nondeterminism lives
  (connecting to service handles, calling models, etc.). Steps cannot reach
  outer-level operations.

**Error contract (important).** Outside of step functions, errors cannot be
meaningfully caught: any workflow error raised by an outer-level operation —
and any error that propagates out of a step function — is persisted to the
execution log at raise time and forces the task into a failed state
(``errored`` for the task's own faults; ``interrupted`` for
not-ready-dependency faults, i.e. ``CellFaultError`` — see `errors.py`),
*even if the task code catches the exception and returns a result anyway*.
Durable workflows require this: a swallowed error could silently persist an
erroneous condition that would not replay cleanly. (It also lets the UI
surface the specific underlying error rather than depending on task code to
re-raise it.) Inside a step function, code is free to catch and handle its
own errors — with one exception: poisoning follows workflow *operations*,
not levels, and the step capability's ``connect`` is a workflow op. A
connect failure poisons the *step invocation* at raise time (its checkpoint
entry is not written even if caught; unbound handle → ``interrupted``,
connecting an unread key → ``errored`` — see `errors.py`). Transient
failures a step
cannot handle should simply propagate (poisoning the task; a rerun replays
completed checkpoints and retries the step); evidential failures (refusals,
parse issues) should be *returned* as data and left to checkers to flag.
The error types live in `errors.py` (re-exported here for convenience).

Everything a task sees is named in its own local vocabulary: arg views carry
the official received names its rule recorded; new names are introduced only
by the task itself (renames, opens, its own coordinates).

These protocols are plain: a task fn is any async ``(ctx, /, **args)``
callable, and likewise a step fn (carrying its declared identity). The
fxtr2 service adaptation — mounting such a callable behind a ``ServiceRef``
so it executes across the runner boundary — lives in the adapter classes
(:class:`task_service.TaskFnAdapter`, :class:`view_runtime.StepFnAdapter`),
which are also exposed as the ``@task_fn`` / ``@step_fn(type_id=...)``
decorators there: decorating a callable *is* wrapping it in the adapter, and
because each adapter is itself such a callable, plain functions, class
instances with ``__call__``, and the adapters all satisfy the same protocol.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping, Protocol, Union

from fxtr2.common.channels import FxtrChannelWriter
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import ServiceRef
from fxtr2.services.service_cap_common import FxtrServiceData
from fxtr2.services.service_dialect import OverrideThisTypeBeforeUsing

if TYPE_CHECKING:
    import polars as pl

from fxtr_experiment_task.errors import (
    CellFaultError,
    CheckpointCorruptionError,
    GridConfigMismatchError,
    HandleUnboundError,
    NondeterminismError,
    ReadWriteExclusionError,
    ViewAxisError,
    WorkflowError,
    WriteConflictError,
)
from fxtr_experiment_task.views import GridView, SubtreeView

__all__ = [
    "CellFaultError",
    "CheckpointCorruptionError",
    "GridConfigMismatchError",
    "HandleUnboundError",
    "NondeterminismError",
    "ReadWriteExclusionError",
    "StepContext",
    "StepFn",
    "StepFnLike",
    "TaskContext",
    "TaskFn",
    "TaskFnLike",
    "ViewAxisError",
    "WorkflowError",
    "WriteConflictError",
]


class TaskContext(Protocol):
    """Capability a task fn's outer level runs against.

    The task's rule-declared args do **not** come through the context — they
    are passed to :meth:`TaskFn.__call__` as keyword arguments. The context
    carries only what exists outside the arg list (the task's private
    internals and outer-level logging; likely later: introspection).
    """

    def own_internals(self) -> SubtreeView:
        """This task cell's private internals key-space (durable internal state).

        Returns:
            A zero-axis :class:`~fxtr_experiment.views.SubtreeView`; checkpoint
            grids are opened here via
            :meth:`~fxtr_experiment.views.SubtreeView.open_checkpoint`.
        """
        ...

    async def log(self, message: str) -> None:
        """Emit a human-facing outer-level progress/log line (not
        load-bearing). The outer-level counterpart to
        :meth:`StepContext.log`: lands in the cell's runtime-only run
        log with no step identity, and is surfaced live on the cell's
        watch (`notes/event_system.md`). Records nothing; silent on
        replay (the safety pass writes to a discarded sink).

        Args:
            message: The line to log.
        """
        ...

    async def open_channel(self) -> FxtrChannelWriter:
        """Open a structured output channel for this task.

        Richer counterpart to :meth:`log` for streaming structured
        progress / output (fxtr2 output-channel tree). Part of the API
        to track the requirement; plumbing is deferred (see
        ``plans/phase4_monitoring_ui/chunk7_part2_channels.md``) — string
        :meth:`log` is the first implemented path.

        Returns:
            A channel writer scoped to (at most) the task run.
        """
        ...


class TaskFn(Protocol):
    """A task function: the outer, deterministic/idempotent level of a task.

    Referenced from task cells via a handle-grid cell (its content-addressed
    handle entity is the code identity that staleness and migrations track).
    The entire ``__call__`` must be observably deterministic given its recorded
    grid operations; in strict mode it is re-run after completion and every
    operation it attempts must already be recorded/cached
    (:class:`~fxtr_experiment.errors.NondeterminismError` otherwise). See the
    module docstring for the error contract: workflow errors raised here
    poison the task even if caught.
    """

    async def __call__(
        self, ctx: TaskContext, /, **args: GridView
    ) -> "Mapping[str, FxtrID | pl.DataFrame] | FxtrID | pl.DataFrame":
        """Execute the task.

        Args:
            ctx: The task context (positional-only).
            **args: The rule's named arg views, one keyword per rule input.
                Each view arrives pre-sliced (vmap-consumed axes are gone —
                the task cannot tell what they were bound to, nor which
                subtree it runs in) with the official received axis names.
                Access modes travel with the views; handle args arrive as
                :class:`~fxtr_experiment.views.HandleView`\\ s whose keys the
                outer level reads (``read_key``) and passes into steps,
                where they can be connected.

        Returns:
            One value per declared output, keyed by output name: an entity id
            for ``EntityOutput`` slots, a ``polars.DataFrame`` (matching the
            declared schema — validated at the boundary) for ``FrameOutput``
            slots. All declared outputs must be
            filled. Convenience: a task whose grid declares *exactly one*
            output may return the bare value, which is wrapped under that
            output's name (whatever it is — this pairs with the omitted-
            ``outputs`` default of a single ``EntityOutput`` named
            ``"result"``, and with no-arg ``TaskView.output()``).
        """
        ...


TaskFnLike = Union[TaskFn, Callable[..., "Awaitable[Any]"]]
"""What the task-fn adapter/decorator accepts.

A *concrete* task fn names its rule's args in its signature
(``async def f(ctx, /, *, inp)``) — statically **narrower** than
:class:`TaskFn`'s ``**args`` form, hence not assignable to the protocol —
so entry points accept any async callable. The binding between the
signature's names and the rule's arg names, and the return shape
(``normalize_outputs``), are enforced at call time — the only place they
can be; the protocol remains the documented contract.
"""


class StepContext(Protocol):
    """Capability a step fn runs against — the only gateway to nondeterminism.

    Lives only as long as the step invocation; everything reachable from it is
    cut off from outer-level operations.
    """

    async def connect(self, handle: FxtrID) -> ServiceRef[OverrideThisTypeBeforeUsing]:
        """Exchange a previously-read handle key for a live service.

        Records no observation — the identity observation happened when the
        outer level read the key
        (:meth:`~fxtr_experiment.views.HandleView.read_key`), and connect
        authority follows that record: only keys the current generation has
        read may be connected.

        Args:
            handle: A handle key (handle entity id), typically received
                through the step's checkpointed args.

        Returns:
            A service ref for the connected service, alive at most as long as
            the step invocation. The ref's service type is unresolved — call
            ``replace_service_type`` with the expected protocol before use.

        Raises:
            HandleUnboundError: No live binding for the key in this runner —
                poisons this *step invocation* (checkpoint entry not written,
                even if caught) and interrupts the task; bind the handle and
                re-demand.
            WorkflowError: The key was not read from a handle grant by the
                current generation (connect authority follows the record;
                ids read from data cells confer nothing) — an integrity
                violation (``errored``).
        """
        ...

    async def log(self, message: str) -> None:
        """Emit a human-facing progress/log line (not load-bearing).

        Args:
            message: The line to log.
        """
        ...

    async def open_channel(self) -> FxtrChannelWriter:
        """Open a structured output channel for this step.

        Richer counterpart to :meth:`log` for streaming structured progress /
        output (fxtr2 output-channel tree). Part of the API to track the
        requirement; plumbing is deferred — string :meth:`log` is the first
        implemented path.

        Returns:
            A channel writer scoped to (at most) the step invocation.
        """
        ...


class StepFn(Protocol):
    """A step: one durable, nondeterministic unit of work.

    Steps are the retry unit — a transient/environment failure should just
    propagate out (poisoning the task) and be retried by re-running the
    (idempotent) outer level, which replays completed checkpoints and
    re-attempts this one. Unlike the outer level, a step may freely catch and
    handle its *own* internal errors (``connect`` failures excepted — see the
    module docstring). Evidential failures (refusals, parse issues) should be
    *returned* as data and left to checkers to flag.
    """

    async def __call__(self, ctx: StepContext, /, **args: FxtrServiceData) -> FxtrServiceData:
        """Execute the step.

        Args:
            ctx: The step context (positional-only).
            **args: The checkpoint-recorded step arguments, exactly as passed
                to :meth:`~fxtr_experiment.views.CheckpointView.run_step`
                (replay verifies the invocation matches the stored entry).

        Returns:
            The step result, recorded in the checkpoint entry.
        """
        ...


StepFnLike = Union[StepFn, Callable[..., "Awaitable[FxtrServiceData]"]]
"""What the step-fn adapter/decorator accepts — see :data:`TaskFnLike`
(the same concrete-signature-vs-``**args`` widening, for steps)."""
