"""Workflow error types and the error-poisoning contract.

**Poisoning contract.** The durable-workflow discipline forbids catching
errors anywhere except *inside* a step function. Any workflow error raised by
an outer-level operation (grid reads / writes / demands / opens, view misuse,
checkpoint operations), and any error that propagates out of a step function,
is persisted to the execution log **at raise time** and forces the running
task into a *failed* state — even if task code catches the exception and
goes on to return a result. Rationale: if catching an error could suppress
it, task logic could silently persist an erroneous condition that would not
replay cleanly. A side benefit is that the UI can surface the specific
underlying error(s) that occurred, rather than relying on task code to
re-raise them faithfully.

**Failed states have two flavors**, split by whose fault the failure is:
``interrupted`` when the task wanted something that isn't ready
(:class:`CellFaultError` — a missing, errored, interrupted, stale, or
unsettled subject) or its run was cut off externally (crash/shutdown), and
``errored``
for everything else (the task's own logic raised, an error escaped a step,
or an integrity violation: write conflict, config mismatch, axis misuse,
nondeterminism). Both fault consumers who read the cell identically; they
surface differently at the top level, so a single broken input renders as
one root cause plus a tree of interrupted dependents rather than cascading
errors.

Step functions are the nondeterministic level and may catch and handle their
*own* errors internally (e.g. retrying a flaky service call). Poisoning
follows workflow *operations*, not levels: the one workflow op available
inside a step is ``StepContext.connect``, and an error it raises poisons the
**step invocation** at raise time — the checkpoint entry is not written, and
the failure stands even if step code catches the exception and returns a
value — failing the task per the error's class
(:class:`HandleUnboundError` is a not-ready dependency → ``interrupted``;
connecting a key the generation never read is an integrity violation →
``errored``). Any other error a step cannot handle should simply propagate
out — poisoning the task — and be retried by re-running the task
(checkpoints replay). Evidential failures (refusals, parse issues) should
instead be *returned* as data and left to checkers to flag.
"""

from __future__ import annotations

from typing import Awaitable, TypeVar

_T = TypeVar("_T")


class WorkflowError(Exception):
    """Base class for workflow-layer errors.

    All workflow errors participate in the poisoning contract (see module
    docstring): raising one from an outer-level operation persists it and
    marks the running task ``errored``, whether or not it is caught.
    """


class CellFaultError(WorkflowError):
    """A read/demand hit a cell that is not ready (missing, errored,
    interrupted, stale, or unsettled).

    Faults propagate as interruptions: the reading task cannot observe the
    subject and itself fails to execute — its cell becomes ``interrupted``
    (not ``errored``) for downstream readers, since the failure is a
    not-ready dependency rather than a defect in the raising task. Poisons
    the raising task (see module docstring).
    """


class HandleUnboundError(CellFaultError):
    """A step connected to a handle key with no live binding in this runner.

    A not-ready dependency, not a defect: external handle bindings are
    *untracked runner-process configuration*, so a result computed around a
    missing binding could never be staleness-invalidated once the binding
    appears — it must not be persisted at all. Hence this poisons the raising
    *step invocation* (its checkpoint entry is not written, even if step code
    catches this and returns a value) and interrupts the task — the same
    semantics as reading a cell that isn't there. Bind the handle and
    re-demand; completed checkpoints replay.
    """


class RequestAbortedError(CellFaultError):
    """A committed op was cancelled before it resolved.

    Never raised into task code (the requester sees its own cancellation);
    this class exists as the failure record the engine writes when a
    request is cut off *after* its commitment point — whether the task
    dropped/cancelled it internally or the whole run was cancelled
    externally. A fault: the task requested something that never became
    ready (request = observation), so the run can only end ``interrupted``
    — the aborted op recorded no observation, and a later run re-attempts
    it at the frontier. Nothing waits on the aborted work itself.
    """


class WriteConflictError(WorkflowError):
    """A write targeted a cell already populated with a *different* entity.

    Writing the identical entity id is a no-op success (idempotent replay).
    Poisons the raising task (see module docstring).
    """


class TargetConflictError(WorkflowError):
    """``add_target`` found a record for this slice with a different config.

    Targets are one record per slice identity: re-adding the identical
    record is an idempotent no-op, but a stale record (pinning an earlier
    expected config) must be removed before the slice can be re-targeted —
    intent changes are explicit, never silently rebound. A top-level
    (user-surface) error, raised interactively; task code never targets.
    """


class UngroundedSubjectError(CellFaultError):
    """The strict grounding gate refused to serve an ungrounded subject.

    A new observation would have depended on a fact that does not trace
    to current user intent (``notes/grounding.md``: orphaned — no
    claimant at all — or unanchored — claimed, but the chain does not
    derive). A fault-class error like every not-ready dependency: the
    remedy is intent maintenance (target / source / adopt the roots the
    diagnosis names), then re-demand. Requests are self-vouching along
    the in-flight demand chain, so this never fires merely because the
    demander's own bookkeeping is pending.
    """


class NotTargetedError(WorkflowError):
    """A user demand addressed a task cell with no attached target.

    User demands require targets (``notes/grounding.md``, maintaining the
    root facts): the method called declares the intent, and the current
    state — e.g. the cell being wanted through some *other* task's
    records — never substitutes for a declaration the user didn't make.
    Values that merely exist are looked at through the labeled
    introspection channels. The `Workflow` demand wrapper auto-adds the
    target by default, so this surfaces on ``target=False`` demands and
    raw capability reads. A top-level (user-surface) error, raised
    interactively; task demands are self-vouching and never see it.
    """


class GridNotSourcedError(WorkflowError):
    """Cell-level user intent requires grid-level user sourcing first.

    The grid-before-cell invariant (``notes/grounding.md``): targets,
    cell sourcing records, and user writes (writing is sourcing) all
    presuppose a user sourcing record for the grid — created automatically
    by user creates, or explicitly for grids the user did not create
    (*adoption*). A top-level (user-surface) error, raised interactively;
    task code never touches sourcing.
    """


class HandleAlreadyBoundError(WorkflowError):
    """A bind was refused because the handle key is already bound.

    A key is bound in at most one class — ephemeral (``bind_handle``) or
    sandboxed (``bind_sandbox``) — at a time; whichever registration comes
    second is refused, in *either* order (no shadowing, no precedence to
    reason about — ``notes/sandboxed_bindings.md``). Release the existing
    binding first (``release()`` for ephemeral, ``remove_sandbox_binding``
    for sandboxed). A top-level (user-surface) error, raised interactively;
    task code never binds.
    """


class SandboxDependencyError(WorkflowError):
    """A sandboxed binding's dependency cell was not bindable at bind time.

    ``bind_sandbox`` pins each dependency to a scalar *handle* cell's
    current key: the cell must exist, be populated, and belong to a handle
    grid. An empty cell, a missing grid, or a non-handle selection is
    refused here (the message names the offending dependency). Distinct
    from the connect-time pin re-check (a not-ready fault — chunk 3): this
    is the interactive failure of establishing the pin in the first place.
    A top-level (user-surface) error.
    """


class SandboxRepoUnconfiguredError(CellFaultError):
    """A sandbox-bound key was demanded with no sandbox repo configured.

    Sandboxed bindings resolve their pinned ``git_commit``\\ s against one
    runner-configured repo (``open_local(sandbox_repo=…)`` — ephemeral,
    machine-local state, exactly parallel to ephemeral bindings needing
    re-registration after reopen/import). Without it the records are
    inert. A not-ready fault, same family as an unbound handle with a
    different remediation: configure the repo and re-demand.
    """


class BindingDependencyFaultError(HandleUnboundError):
    """A binding's pinned dependency was not servable at connect time.

    Every connect re-reads each pinned dependency cell and checks it
    against the pin (``notes/sandboxed_bindings.md``): an empty cell, a
    missing grid/cell, or a **pin mismatch** (the cell now holds a
    different key — an identity-level declaration that behavior changed)
    faults here, naming the dependency, the pin, and what was found. A
    not-ready fault in the unbound-handle family: fix the dependency
    cell (or remove + re-bind to re-pin — the explicit "I assert
    compatibility" act) and re-demand.
    """


class BindingCycleError(CellFaultError):
    """Resolving a binding's dependencies re-entered a key already being
    served (a dependency cycle among bindings).

    Dependency resolution is eager and recursive — a pinned dependency
    may itself be sandbox-bound — so a cycle would recurse forever;
    the in-progress set detects it and faults, naming the cycle path.
    A not-ready fault: re-bind the offending binding without the cycle
    and re-demand.
    """


class SandboxLaunchError(CellFaultError):
    """Launching a sandboxed binding's process failed.

    Covers the whole launch pipeline — nix eval/build (a missing commit
    in the configured repo, a broken flake, a missing or unknown attr
    ``kind``), image delivery, and process spawn. The underlying
    message (nix stderr included, unbounded) is carried verbatim.
    A not-ready fault: launches are re-attempted per demand (no negative
    caching in v0), so fix the flake/repo and re-demand.
    """


class UnknownBindingError(WorkflowError):
    """``remove_sandbox_binding`` addressed a key with no sandboxed binding.

    Removal is an explicit act, so an unknown key is surfaced rather than
    silently ignored (unlike ``remove_target``, which tolerates dangling
    records). A top-level (user-surface) error, raised interactively.
    """


class ReadWriteExclusionError(WorkflowError):
    """A generation performed both reads and writes on one grid.

    The read-xor-write exclusion is op-level, per generation, per grid (see
    `model.SliceMode`): reading a grid you wrote is vacuous (you can only
    see what you wrote), writing a grid you read is a latent self-conflict.
    An integrity violation — poisons the raising task as ``errored``.
    """


class GridConfigMismatchError(WorkflowError):
    """An open targeted an existing grid/key with a different canonical config.

    Covers direct config mismatch, subtree regularity violations (same key
    opened with different configs across cells — including per-cell-varying
    wiring, which canonicalizes to different configs), and received-name
    disagreements (names a task fn observes are identity-bearing).
    Poisons the raising task (see module docstring).
    """


class TaskOutputError(WorkflowError):
    """A task fn's return did not satisfy its grid's declared outputs.

    Raised by the task-side adapter when normalizing the return: a bare
    (non-mapping) return from a multi-output grid, missing/extra output
    names, or a value of the wrong shape for its slot. An integrity
    violation — poisons the task as ``errored``.
    """


class TaskRaisedError(WorkflowError):
    """An exception escaped the task fn with no engine-recorded failure.

    Recorded by the *executor* as the generation's failure entry when a run
    raises without the engine having persisted any failure — by
    construction the task's own logic raised, since every engine-op failure
    is recorded engine-side at raise time. Classified ``errored``. The
    escaped exception's transit form (class name, message) is kept in the
    failure record for display only; it is never used for classification.
    """


class ViewAxisError(WorkflowError):
    """A rewrite or operation misused a view's axes.

    Raised for: renaming a nonexistent axis, renaming onto an existing name,
    supplying coordinates for unknown axes, leaving axes unresolved where a
    single cell is required, or an axis-name collision when an open appends a
    child grid's axes to the view. Poisons the raising task (see module
    docstring) — even though pure rewrites are otherwise unrecorded, their
    failures are not recoverable in-task.
    """


class StepRaisedError(WorkflowError):
    """An error escaped a step fn.

    Recorded by the ``run_step`` miss path when the invoked step service
    raises: the poisoning contract persists it at raise time (no
    checkpoint entry is written) and the task ends ``errored`` — steps
    are the retry unit, so a later run replays completed checkpoints and
    re-attempts this one. The escaped exception's transit form (class
    name, message) is kept in the failure record for display only.
    """


class StepInvocationFailedError(WorkflowError):
    """A step invocation was poisoned; this signals the failure to the task.

    Raised by ``run_step`` toward the demanding task when the invocation's
    poisoned flag is set (a ``connect`` failure occurred), whether the
    step let the error escape or caught it and returned a value. Two
    events, two exceptions: the *poisoning* was registered exactly once,
    at the originating raise site inside ``connect`` (typed, classified,
    persisted at raise time); this exception is the *signal* — constructed
    fresh, chained ``from`` the cause for context, and by contract **never
    recorded as a failure** (the engine's recording layer skips this type;
    its cause's row already exists and carries the fault/errored split).
    """


class StepPayloadError(WorkflowError):
    """A step's args or result contained non-durable or malformed data.

    Step payloads are entity-encoded (the recorded invocation and the
    checkpointed result must survive replay), so they admit exactly the
    durable subset of the service vocabulary: primitives, ``str``-keyed
    mappings, sequences, and entity ids (``FxtrID`` / ``EntityRef`` —
    collapsed to bare ids, matching the wire). ``ServiceRef``\\ s are
    rejected: refs are not durable, and recording one is meaningless on
    replay — services cross into steps as handle *keys* exchanged via
    ``connect``. An integrity violation — poisons the task as
    ``errored``.
    """


class DuplicateStepError(WorkflowError):
    """A run invoked ``run_step`` twice for the same checkpoint cell.

    Each checkpoint cell admits one ``run_step`` call per run — a second
    call, concurrent or sequential, is an integrity violation
    (``errored``); deduplicating identical work belongs in task logic.
    (Beyond the simpler mental model, this closes two holes: concurrent
    misses would execute the step twice, and the safety pass's *set*
    comparison cannot see call multiplicity.) Across runs the rule is
    per-run: a resumed run's single call is served from the recorded
    entry as usual.
    """


class ConnectAuthorityError(WorkflowError):
    """A step connected a key its generation never read from a handle grant.

    Connect authority follows the record: only keys the current
    generation has read via a *handle-grant* read may be connected —
    authority never flows through data, so an id fished out of a data
    cell confers nothing (the sanctioned way to hand connect-ability
    onward is granting the handle view itself). An integrity violation —
    poisons the *step invocation* (no checkpoint entry, even if step code
    catches this) and the task as ``errored``.
    """


class CheckpointCorruptionError(WorkflowError):
    """A checkpoint cell holds an entry bound to a foreign generation.

    Should never happen: generation turnover must atomically delete old-
    generation entries before a new generation starts. Poisons the raising
    task (see module docstring).
    """


class NotAtRestError(WorkflowError):
    """An at-rest operation was attempted while a generation is running.

    Export (and import) treat the layout of ``notes/at_rest_format.md``
    as an at-rest format: refusing while anything runs is what lets it
    ignore transient mid-run state entirely. Interactive — retry when the
    workflow is quiescent.
    """


class ExportLayoutError(WorkflowError):
    """The export directory's existing content is not export-shaped.

    The export dir is wholly export-owned, and ownership is *proved*, not
    assumed: every non-dot entry must match the known layout (the
    manifest, the in-progress marker, per-grid reserved files, valid-key
    directories) or the operation refuses before touching anything — a
    misdirected ``export_state`` is a safe error, never a destructive
    sweep. Interactive — point the export at its own directory.
    """


class ImportFormatError(WorkflowError):
    """The import source is not a well-formed at-rest layout.

    Covers everything shape-level, named with file (and row): a missing
    or version-mismatched manifest, a present ``EXPORT_IN_PROGRESS``
    marker (a torn export), unknown entries, malformed/unsorted/empty
    TSVs, bad id strings, a checked-redundant column disagreeing with
    its decoded truth, or a violated cross-row law. Raised before
    anything is written. Interactive.
    """


class MissingEntitiesError(WorkflowError):
    """Ids the import layout references are absent from the entity store.

    The layout pairs with a *specific* shared store (the extraction
    contract of ``notes/at_rest_format.md``); importing against the
    wrong store — or an incompletely synced one — surfaces here, with
    every missing id listed, before anything is written. Interactive.
    """


class StateNotEmptyError(WorkflowError):
    """Import refused: the state db already holds state.

    Import into empty is the default contract; replacing existing state
    is the explicit ``force=True`` wipe (export first — git is the
    backup). Interactive.
    """


class NondeterminismError(WorkflowError):
    """A task's outer level was caught behaving non-deterministically.

    Raised when the safety replay attempts an operation that is not already
    recorded/cached (or fails to attempt one that is), and when a checkpoint
    replay finds the current invocation (step fn identity or args) differs
    from what the stored entry recorded. Poisons the raising task (see module
    docstring).
    """


class InternalRunnerError(RuntimeError):
    """A runner invariant was violated — a bug in the system, not in task
    code. Deliberately *not* a ``WorkflowError``: it takes the
    unexpected-internal-error poisoning path (recorded as an error-class
    failure with this class name), and its appearance in a failure record
    means the runner itself needs fixing."""


class EncodingError(ValueError):
    """A value violated the canonical-encoding contracts (bad type/shape).

    Raised by the encoding funnels (`encoding.py`, `frame_encoding.py`) and
    the frame write boundary (`tables.py`). Deliberately *not* a
    ``WorkflowError``: it is a plain value-validation failure that predates
    and sits outside the poisoning taxonomy — the ops that can hit one
    catch it and re-raise the taxonomy's own type (e.g. the executor's
    frame-output canonicalization). It lives in this module rather than
    with the encoders because it is the one thing both the identity
    encoders and the task-side frame runtime need, and the taxonomy module
    is the boundary both sides may import.
    """


class GatherFailedError(WorkflowError):
    """A gather's batch failed — a *signal* naming already-handled causes.

    Gather resolves its cells concurrently with per-request failure
    semantics (each failing cell is its own fact: on the run capability,
    recorded as its own failure row at raise time, classified from its
    own typed exception — exactly what k parallel single reads would
    record; on the user capability, nothing records). The one exception
    the *op* then raises is deliberately synthesized: it lists every
    failed cell with its error class and message, and is chained from the
    task group's exception group so tracebacks keep the full picture.

    Deliberately *not* a :class:`CellFaultError` — the batch's causes may
    mix fault- and error-class, and the signal declines to summarize them
    (the per-cell classes are in the message and, in-process, the chained
    group). The fault/error distinction keeps its full force where it is
    systemic: the per-cell failure rows, from which the generation's
    terminal status derives. If an interactive consumer ever needs to
    *branch* on the aggregate, adding a fault-class subtype (raised for
    all-fault batches) is a purely additive change.

    Like :class:`StepInvocationFailedError`, this type is never itself
    persisted as a failure (its causes' rows already exist runner-side);
    the generation's errored/interrupted classification comes from the
    per-cell rows, never from this signal.
    """


# -- transit re-raise ----------------------------------------------------------
#
# Errors that cross a service boundary travel as fxtr2 ``IRException`` with
# the original class name as ``category`` — informational only (nothing
# durable is ever classified from a transit form; see
# ``notes/design_overview.md``). The consuming boundaries (the view
# runtime's effectful ops and the ``Workflow`` wrapper) still owe their
# callers the documented *typed* exceptions, so they route wire calls
# through :func:`typed_wire_call`, which reconstructs the type purely for
# ergonomics.


def _transit_types() -> "dict[str, type[WorkflowError]]":
    """Every workflow error type by name (the transit ``category`` key)."""
    out: dict[str, type[WorkflowError]] = {}
    stack: list[type[WorkflowError]] = [WorkflowError]
    while stack:
        cls = stack.pop()
        out[cls.__name__] = cls
        stack.extend(cls.__subclasses__())
    return out


async def typed_wire_call(call: Awaitable[_T]) -> _T:
    """Await one wire call, re-raising known workflow errors typed.

    The reconstruction is chained from the transit exception (so the
    original traceback context survives) and carries its display string;
    unknown categories — including :class:`InternalRunnerError`, which is
    deliberately *not* a :class:`WorkflowError` — pass through untouched.
    Never used for anything durable: raise-time poisoning persistence and
    the errored/interrupted classification stay runner-side.

    Args:
        call: The awaitable of one proxy method invocation.

    Returns:
        The call's result, unchanged.
    """
    from fxtr2.leases.leased_cap_ir import IRException

    try:
        return await call
    except IRException as exc:
        cls = _transit_types().get(exc.category or "")
        if cls is None:
            raise
        raise cls(exc.message) from exc
