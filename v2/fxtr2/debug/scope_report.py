"""The scope-drain report: print *why* a scope drain is hung.

The report is one global, synchronous dump over the live-scope
registry (`fxtr2.debug.scope_instrumentation`): every **wedged** drain
(a scope parked in ``aclose`` waiting for outstanding leases), the
holders blocking it — structural child scopes with each parent edge
classified (explicit ``include=`` vs implicit active-scope capture;
same-task vs fork-inherited), and labeled bridge leases — plus a
pointed hazard line when a wedged drain's blocker holds it via an
implicit, fork-inherited edge (the classic scope-lifetime deadlock: a
long-lived scope silently adopted an ephemeral ambient scope as a
structural parent, so the ephemeral scope can never finish draining;
see ``v2/notes/scope_discipline.md``). A live-scope inventory follows
as an appendix.

Ways to trigger it:

- ``dump_scope_report()`` — manual, synchronous, callable anywhere
  (including signal handlers and pdb).
- ``fail_after_and_report_hung_scopes(seconds)`` — a ``fail_after``-
  style async CM that at its deadline first dumps (the wedge is still
  pristine — nothing has unwound), then cancels and raises
  ``TimeoutError``. The recommended hang guard for tests.
- ``report_hung_scopes_on_signal()`` — an async CM (wrap your main
  loop in it) that dumps on each delivery of the given signals
  (default ``SIGUSR1``). Nothing is ever installed globally.

Rerun with ``FXTR_SCOPE_REPORT_TB=1`` to add allocation tracebacks to
every lease line and creation tracebacks to every scope (see
``scope_instrumentation``).
"""

from __future__ import annotations

import signal as _signal
import sys
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator, TextIO

import anyio
import anyio.abc

from ..data.entity_ref_scope import FxtrScope
from . import scope_instrumentation as _inst

if TYPE_CHECKING:
    pass

__all__ = [
    "dump_scope_report",
    "fail_after_and_report_hung_scopes",
    "report_hung_scopes_on_signal",
]


def _scope_name(scope: FxtrScope) -> str:
    return scope._name or repr(scope)


def _live_scopes() -> list[FxtrScope]:
    return [s for s in list(_inst.LIVE_SCOPES) if s._started and not s._closed and s.is_open()]


def _is_draining(scope: FxtrScope) -> bool:
    # The documented draining condition: token alive, base auth dropped.
    return scope._scope_auth_lease is None


def _wedged(scopes: list[FxtrScope]) -> list[FxtrScope]:
    """Draining scopes, longest-parked first (unknown park times last)."""
    draining = [s for s in scopes if _is_draining(s)]
    now = _inst.monotonic()

    def parked(s: FxtrScope) -> float:
        return now - s._drain_started_at if s._drain_started_at is not None else -1.0

    return sorted(draining, key=parked, reverse=True)


def _edge_description(child: FxtrScope, parent: FxtrScope) -> tuple[str, bool]:
    """(text, is_hazard) for the child→parent structural edge."""
    if child._outer_scope is parent:
        if child._implicit_capture_cross_task:
            return "IMPLICIT active-scope capture, CROSS-TASK (fork-inherited)", True
        return "implicit active-scope capture, same-task", False
    return "explicit include=", False


def _tags(scope: FxtrScope) -> str:
    if scope._is_dispatch_call_scope:
        return "  [dispatch per-call scope]"
    if scope._is_dispatch_input_scope:
        return "  [dispatch input scope]"
    return ""


def _status(scope: FxtrScope) -> str:
    if not scope.is_open():
        return "ENDED"
    return "draining" if _is_draining(scope) else "OPEN"


def _emit_scope_block(
    out: list[str],
    index: int,
    scope: FxtrScope,
    now: float,
    children_of: dict[int, list[FxtrScope]],
) -> bool:
    """Render one wedged scope; returns whether a hazard line was printed."""
    parked = (
        f"parked {now - scope._drain_started_at:.1f}s"
        if scope._drain_started_at is not None
        else "parked (duration unknown)"
    )
    if scope._lifespan_wait_started_at is not None:
        parked += " (+ waiting on attached lifespans)"
    out.append(f"\n{index}. '{_scope_name(scope)}'{_tags(scope)}  {parked}")
    if scope._creation_site is not None:
        out.append(f"   created: {scope._creation_site}")
    if scope._creation_tb is not None:
        out.append("   creation traceback (tail):")
        out.extend(f"     {line}" for line in scope._creation_tb.splitlines()[-6:])

    hazard = False
    derived = scope._derived
    n_outstanding = len(derived._output_tracking) if derived is not None else 0
    out.append(f"   blocked on {n_outstanding} outstanding lease(s) of its main token:")

    # Structural holders: open child scopes (their token holds a parent-via
    # lease on ours). Classified edges are the diagnosis.
    for child in children_of.get(id(scope), ()):
        edge_text, edge_hazard = _edge_description(child, scope)
        out.append(
            f"     - parent-via lease held by scope '{_scope_name(child)}'"
            f" ({_status(child)}{'' if _status(child) != 'OPEN' else ' — will not end before this scope can'})"
        )
        out.append(f"       edge: {edge_text}")
        if child._creation_site is not None:
            out.append(f"         child created at {child._creation_site}")
        if edge_hazard:
            hazard = True

    # Labeled + unresolved leases from the tracking dict.
    if derived is not None:
        for info, _finalizer in list(derived._output_tracking.values()):
            if info.holder_label is not None:
                out.append(f"     - lease '{info.holder_label}'")
            else:
                out.append(f"     - {info.lease_repr}")
            if info.alloc_tb is not None:
                out.append("       allocated at (tail):")
                out.extend(f"         {line}" for line in info.alloc_tb.splitlines()[-6:])

    if hazard:
        out.append(
            "   !! hazard: a longer-lived scope implicitly derives from this"
            " scope via a fork-inherited active-scope capture"
            " (see v2/notes/scope_discipline.md)"
        )
    return hazard


def build_scope_report(trigger: str) -> str:
    """Build the scope-drain report text (see the module docstring).

    Pure and synchronous — reads the live-scope registry and each
    scope's instrumentation fields, allocates nothing durable, and
    never awaits, so it is safe at cancellation boundaries and inside
    signal receivers.

    Args:
        trigger: Short human-readable statement of what asked for the
            report (e.g. ``"guard deadline (30s) reached"``); rendered
            in the header.

    Returns:
        The full report as one newline-joined string: the wedged-drain
        blocks (blockers, classified edges, hazard lines) followed by
        the live-scope inventory.
    """
    now = _inst.monotonic()
    scopes = _live_scopes()
    wedged = _wedged(scopes)
    children_of: dict[int, list[FxtrScope]] = {}
    for s in scopes:
        for p in s._parents:
            children_of.setdefault(id(p), []).append(s)

    out: list[str] = []
    out.append("======== fxtr2 scope-drain report ========")
    out.append(f"trigger: {trigger}")
    if not wedged:
        out.append("No scope drains are currently parked.")
    else:
        out.append(f"\nWedged drains ({len(wedged)}):")
        for i, scope in enumerate(wedged, 1):
            _emit_scope_block(out, i, scope, now, children_of)

    out.append(f"\nLive-scope inventory ({len(scopes)} scopes):")
    for s in sorted(scopes, key=lambda s: s._created_at):
        n_leases = len(s._derived._output_tracking) if s._derived is not None else 0
        implicit = "+implicit outer" if s._outer_scope is not None else ""
        out.append(
            f"  - '{_scope_name(s)}'{_tags(s)}  {_status(s)}"
            f"  age {now - s._created_at:.1f}s"
            f"  parents {len(s._parents)}{implicit}  leases {n_leases}"
        )
    if not _inst.CAPTURE_CREATION_TB:
        out.append(
            "======== end report (rerun with FXTR_SCOPE_REPORT_TB=1 for"
            " allocation/creation tracebacks) ========"
        )
    else:
        out.append("======== end report ========")
    return "\n".join(out)


def dump_scope_report(trigger: str = "manual dump", *, file: TextIO | None = None) -> None:
    """Print the scope-drain report (see the module docstring).

    Strictly synchronous — safe from signal receivers, cancellation
    boundaries, and pdb.

    Args:
        trigger: Short human-readable statement of what asked for the
            report; rendered in the header.
        file: Destination stream; defaults to ``sys.stderr`` (matching
            the lease-leak warnings).
    """
    print(build_scope_report(trigger), file=file or sys.stderr, flush=True)


@asynccontextmanager
async def fail_after_and_report_hung_scopes(
    seconds: float, *, report: bool = True
) -> AsyncIterator[None]:
    """``anyio.fail_after``, except a deadline first dumps the scope-drain report.

    At the deadline, if any scope drain is parked, the full report is
    dumped BEFORE the body is cancelled — the wedge is still pristine,
    so parked durations and holders are exact. If nothing is parked
    (the timeout caught a slow computation, not a wedge) a one-line
    note is printed instead. Then the body is cancelled and
    ``TimeoutError`` raised, like ``fail_after``.

    Semantic deltas vs ``anyio.fail_after`` (this is a timer *task*,
    not a deadline on the cancel scope): entry requires a running task
    group internally (it is an async CM, not a sync one), the deadline
    cannot be rescheduled, and the timer task itself counts toward the
    current task group's children until exit.

    Args:
        seconds: The deadline, measured from CM entry.
        report: If False, restore pure timeout behavior — no report,
            no one-liner (for tests that deliberately exercise hangs).

    Yields:
        Nothing; run the guarded body in the ``async with`` block.

    Raises:
        TimeoutError: The body did not complete before the deadline
            (raised after the report/one-liner is emitted).
    """
    cancel_scope = anyio.CancelScope()

    async def _timer() -> None:
        await anyio.sleep(seconds)
        if report:
            if any(_is_draining(s) for s in _live_scopes()):
                dump_scope_report(trigger=f"guard deadline ({seconds:g}s) reached")
            else:
                print(
                    f"[fxtr2 scope report] guard deadline ({seconds:g}s) reached with no"
                    " parked scope drains — a slow operation, not a lease wedge.",
                    file=sys.stderr,
                    flush=True,
                )
        cancel_scope.cancel()

    async with anyio.create_task_group() as tg:
        tg.start_soon(_timer)
        try:
            with cancel_scope:
                yield
        finally:
            tg.cancel_scope.cancel()
    if cancel_scope.cancelled_caught:
        raise TimeoutError(f"deadline ({seconds:g}s) expired")


@asynccontextmanager
async def report_hung_scopes_on_signal(
    *signals: _signal.Signals,
) -> AsyncIterator[None]:
    """Dump the scope-drain report whenever one of ``signals`` is delivered.

    Opt-in and scoped: nothing is installed globally — wrap your main
    loop in this CM (typically with the default ``SIGUSR1``) to make a
    live, possibly-wedged process inspectable via ``kill -USR1 <pid>``.
    Uses ``anyio.open_signal_receiver``, so it must run in the main
    thread of the main interpreter.

    Args:
        *signals: The signals to dump on; defaults to ``(SIGUSR1,)``
            when empty. Handlers are installed at CM entry and
            restored at exit. Do not pass ``SIGALRM`` in test runs —
            the v2 test conftest owns it for the per-test timeout.

    Yields:
        Nothing; run your main loop in the ``async with`` block.
    """
    sigs = signals or (_signal.SIGUSR1,)

    async def _receiver(*, task_status: anyio.abc.TaskStatus[None]) -> None:
        with anyio.open_signal_receiver(*sigs) as deliveries:
            task_status.started()
            async for signum in deliveries:
                dump_scope_report(trigger=f"signal {_signal.Signals(signum).name}")

    async with anyio.create_task_group() as tg:
        await tg.start(_receiver)
        try:
            yield
        finally:
            tg.cancel_scope.cancel()
