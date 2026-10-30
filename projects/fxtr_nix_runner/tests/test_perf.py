"""The ``perf`` timing helper — inert by default, nesting, task-independent
collection."""

from __future__ import annotations

import anyio
import pytest
from fxtr_nix_runner import perf


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_inert_without_sink_or_logger():
    # No collect_perf region and no stderr handler: spans are pure timing, no
    # events recorded anywhere. (The module logger stays below INFO.)
    assert perf._sinks == []
    with perf.perf_span_sync("noop"):
        pass
    assert perf._sinks == []


@pytest.mark.anyio
async def test_collect_records_nesting_and_order():
    with perf.collect_perf() as events:
        async with perf.perf_span("outer", k="v"):
            async with perf.perf_span("inner"):
                pass
    # Completion order: inner finishes before outer.
    assert [e.name for e in events] == ["inner", "outer"]
    by_name = {e.name: e for e in events}
    assert by_name["outer"].depth == 0
    assert by_name["inner"].depth == 1
    assert by_name["outer"].fields == {"k": "v"}
    # Sorting by start recovers pre-order (parent before child).
    assert [e.name for e in sorted(events, key=lambda e: e.start)] == ["outer", "inner"]


@pytest.mark.anyio
async def test_collect_captures_spans_on_other_tasks():
    # The load-bearing property: a span opened on a separately-started task
    # (mimicking a spawn runner on env_tg) is still captured, because the sink
    # is a module global rather than a contextvar.
    with perf.collect_perf() as events:
        async with anyio.create_task_group() as tg:

            async def child() -> None:
                async with perf.perf_span("on_child_task"):
                    pass

            tg.start_soon(child)
    assert [e.name for e in events] == ["on_child_task"]


def test_sink_removed_after_region():
    with perf.collect_perf():
        assert len(perf._sinks) == 1
    assert perf._sinks == []
