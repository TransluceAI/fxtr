"""Opt-in, low-overhead perf timing for the nix + sandbox-spawn paths.

Public and dependency-light (stdlib only — **no fxtr2**), so both
``fxtr_nix_runner`` and its consumers (e.g. ``fxtr_sandboxing``) can time the
phases of a nix build / a sandbox spawn instead of leaving them a silent wait.

Two consumption modes, both **inert by default**:

- **Structured collection** — wrap a region in :func:`collect_perf` and read
  back the ordered list of :class:`PerfEvent` records (each with a nesting
  ``depth`` and a ``start`` timestamp). The spawn benchmark uses this to render
  a per-phase tree and diff cold vs warm.
- **Stderr logging** — set ``FXTR_PERF_LOG`` truthy (or call
  :func:`enable_perf_stderr_logging`) to print each span's elapsed time,
  indented by nesting, as it completes. Turns any spawn into a visible
  breakdown.

Nesting spans across ``await`` boundaries + anyio task trees:

- ``depth`` (indentation) rides a :class:`~contextvars.ContextVar`, so spans
  opened within one task nest correctly, and a child task started via
  ``TaskGroup.start_soon`` inherits its parent's depth at spawn time.
- The collection **sink is a module-global stack**, *not* a contextvar, on
  purpose: sandbox-spawn work runs on a long-lived ``env_tg`` task group whose
  context was captured when the environment opened — before any per-call
  ``collect_perf`` region exists — so a contextvar sink would miss exactly the
  spans that matter (builder start, image delivery, container run). A global
  sink captures every span regardless of which task it runs on. This assumes
  spans are opened/closed on the event-loop thread (they wrap ``await`` points),
  which holds for all current call sites; it is **not** safe for concurrent
  ``collect_perf`` regions across threads.

When neither a sink nor the logger is active, a span is a ``perf_counter()``
pair plus a couple of contextvar reads — cheap enough to leave in the hot path.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Iterator, Sequence

__all__ = [
    "PerfEvent",
    "perf_span",
    "perf_span_sync",
    "collect_perf",
    "enable_perf_stderr_logging",
    "proc_label",
    "record",
]

logger = logging.getLogger("fxtr.perf")


@dataclass
class PerfEvent:
    """One completed timing span.

    ``depth`` is the span's own nesting level (children are ``depth + 1``);
    ``start`` is a ``time.perf_counter()`` value, so a renderer can sort by
    ``start`` to recover pre-order (parent-before-child) from the
    completion-order list.
    """

    name: str
    elapsed: float
    depth: int
    start: float
    fields: dict[str, object] = field(default_factory=dict)


# Indentation depth for the *current* task (inherited by child tasks at spawn).
_depth: ContextVar[int] = ContextVar("fxtr_perf_depth", default=0)

# Active collection sinks (module-global — see module docstring for why this is
# not a contextvar). Each open `collect_perf` pushes its list; `_emit` appends
# every completed event to all active sinks.
_sinks: list[list[PerfEvent]] = []


def _emit(name: str, elapsed: float, depth: int, start: float, fields: dict[str, object]) -> None:
    if _sinks or logger.isEnabledFor(logging.INFO):
        event = PerfEvent(name=name, elapsed=elapsed, depth=depth, start=start, fields=dict(fields))
        for sink in _sinks:
            sink.append(event)
        if logger.isEnabledFor(logging.INFO):
            indent = "  " * depth
            suffix = "  (" + " ".join(f"{k}={v}" for k, v in fields.items()) + ")" if fields else ""
            logger.info("%s%s  %.3fs%s", indent, name, elapsed, suffix)


@asynccontextmanager
async def perf_span(name: str, **fields: object) -> AsyncIterator[None]:
    """Time an async region as a span named ``name`` (with optional ``fields``).

    The body runs one nesting level deeper, so spans opened inside it (in this
    task or a child task started from it) render as its children.
    """
    depth = _depth.get()
    token = _depth.set(depth + 1)
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        _depth.reset(token)
        _emit(name, elapsed, depth, start, fields)


@contextmanager
def perf_span_sync(name: str, **fields: object) -> Iterator[None]:
    """Synchronous twin of :func:`perf_span` for non-async call sites."""
    depth = _depth.get()
    token = _depth.set(depth + 1)
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        _depth.reset(token)
        _emit(name, elapsed, depth, start, fields)


def record(name: str, **fields: object) -> None:
    """Emit a zero-duration marker event at the current nesting depth.

    For point facts worth surfacing next to the spans (e.g. a poll count, a
    cache hit/miss) without timing a region.
    """
    _emit(name, 0.0, _depth.get(), time.perf_counter(), fields)


@contextmanager
def collect_perf() -> Iterator[list[PerfEvent]]:
    """Collect every span completed during the region into the yielded list.

    Task-independent: captures spans that run on other task groups (e.g. a
    sandbox environment's ``env_tg``) for the duration of the region. Nesting
    regions is fine (each gets its own list). The list is in completion order;
    sort by :attr:`PerfEvent.start` for a pre-order tree.
    """
    sink: list[PerfEvent] = []
    _sinks.append(sink)
    try:
        yield sink
    finally:
        _sinks.remove(sink)


def proc_label(argv: Sequence[str], *, keep: int = 4) -> str:
    """Compact span label for a subprocess ``argv``.

    Keeps the command basename plus up to ``keep - 1`` following *verb-like*
    tokens — dropping flags, flag values, and long operands (container ids,
    store paths, generated names) — so per-process spans read as the operation
    rather than the full (path-heavy, id-heavy) command line. E.g.
    ``["podman", "exec", "-e", "X=y", "<cid>", "nix", "build", …]`` →
    ``"podman exec nix build"``.
    """
    tokens = list(argv)
    if not tokens:
        return "<empty>"
    parts = [Path(tokens[0]).name]
    for tok in tokens[1:]:
        if len(parts) >= keep:
            break
        if tok.startswith("-") or "/" in tok or "=" in tok or len(tok) > 12:
            continue
        parts.append(tok)
    return " ".join(parts)


_stderr_handler_installed = False


def enable_perf_stderr_logging(level: int = logging.INFO) -> None:
    """Attach a compact stderr handler to the ``fxtr.perf`` logger.

    Idempotent. Called automatically at import when ``FXTR_PERF_LOG`` is truthy;
    call it explicitly to turn on the breakdown from code. Sets
    ``propagate = False`` so a configured root logger doesn't double-print.
    """
    global _stderr_handler_installed
    logger.setLevel(level)
    logger.propagate = False
    if _stderr_handler_installed:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[perf] %(message)s"))
    logger.addHandler(handler)
    _stderr_handler_installed = True


if os.environ.get("FXTR_PERF_LOG", "").lower() not in ("", "0", "false", "no"):
    enable_perf_stderr_logging()
