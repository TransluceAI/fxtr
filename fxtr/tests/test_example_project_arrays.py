"""Runs the example project's project arrays walkthrough against a real database, through the
local client over a fake environment."""

from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy")

from postgres_testing import a_client  # noqa: E402

from basic_project.project_arrays import note_title, note_titles, walkthrough  # noqa: E402
from fxtr.runner.postgres.database import Database  # noqa: E402

pytestmark = pytest.mark.anyio


async def test_walkthrough_runs(database: Database, capsys: pytest.CaptureFixture[str]) -> None:
    async with a_client(database, note_titles, note_title) as client:
        await walkthrough(client)
    out = capsys.readouterr().out
    assert "same snapshot -> True" in out
    assert "new entity -> True" in out
    assert "the same entity as the first -> True" in out
    assert ": succeeded" in out
    assert "root input 'notes' is the snapshot -> True" in out
