"""Shared fixtures for behaviors tests."""

import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
