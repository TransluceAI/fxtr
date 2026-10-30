"""Shared fixtures and helpers for fxtr_workflow tests."""

import dataclasses
import uuid
from pathlib import Path

import pytest

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities
from fxtr_workflow.cache_slot import LiveCacheSlot, _Backend

# ---------------------------------------------------------------------------
# Shared test entity type for step keys
# ---------------------------------------------------------------------------

_TEST_NS = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


@dataclasses.dataclass(frozen=True)
class StepKeyEntity(
    DataclassEntity,
    fxtr_type=system_entities.define_type(_TEST_NS, "test_step_key"),
):
    label: str


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """SQLite database URL for testing."""
    return f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"


# ---------------------------------------------------------------------------
# Helpers for resolve-level tests (no real namespace needed)
# ---------------------------------------------------------------------------


class DummyBackend(_Backend):
    def dispatch_in_slot(self, slot, step, args, kwargs):
        raise NotImplementedError


def make_dummy_slot(pin_key: str = "test") -> LiveCacheSlot:
    return LiveCacheSlot(None, pin_key, DummyBackend())  # type: ignore[arg-type]
