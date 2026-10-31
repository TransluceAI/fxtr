"""Smoke tests confirming the package imports and the async test setup works."""

from __future__ import annotations

import anyio
import pytest


def test_import_package() -> None:
    import fxtr

    assert fxtr is not None


@pytest.mark.anyio
async def test_anyio_runs() -> None:
    result: list[int] = []

    async def _produce() -> None:
        await anyio.sleep(0)
        result.append(1)

    async with anyio.create_task_group() as tg:
        tg.start_soon(_produce)

    assert result == [1]
