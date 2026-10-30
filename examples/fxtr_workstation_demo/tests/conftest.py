"""Pytest setup for the demo's host-side tests.

The agent packages under ``agent_workdir/packages/`` are marker-style /
wasm packages, not installed into this venv — make them importable the
same way ``run_demo_workstation.py`` does, via ``sys.path`` inserts
against their ``src/`` layouts. The embedding venv supplies full fxtr2
(see ``agent_workdir/packages/README.md``, context 2).
"""

from __future__ import annotations

import gc
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

_AGENT_PACKAGES = Path(__file__).resolve().parents[1] / "agent_workdir" / "packages"
for _pkg in (
    "presentables",
    "transcripts",
    "simple_tasks",
    "simple_task_lib",
    "prompt_task",
    "simple_scheduler",
    "streaming_tasks",
    "streaming_task_lib",
    "streaming_scheduler",
):
    sys.path.insert(0, str(_AGENT_PACKAGES / _pkg / "src"))

from fxtr2.leases import local_lease  # noqa: E402


@pytest.fixture(autouse=True)
def _assert_no_lease_leaks() -> Iterator[None]:
    """Fail the test if any lease is GC'd without explicit discard during
    its run (same guard as v2/tests/conftest.py) — catches e.g. a
    retained run lease that the scheduler forgot to drop."""
    gc.collect()
    before = local_lease.get_lease_leak_count()
    yield
    gc.collect()
    delta = local_lease.get_lease_leak_count() - before
    if delta:
        pytest.fail(f"Test leaked {delta} lease(s) without discard; see warnings in log.")
