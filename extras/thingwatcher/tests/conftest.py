"""Shared test fixtures and utilities."""

from __future__ import annotations

import asyncio
import socket
from contextlib import closing
from typing import Any

import pytest
from httpx import AsyncClient


def find_free_port() -> int:
    """Find a free port on localhost."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


@pytest.fixture
def free_port() -> int:
    """Fixture that provides a unique free port for each test."""
    return find_free_port()


async def wait_for_server(port: int, timeout: float = 5.0) -> None:
    """Wait for the server to be ready."""
    import httpx

    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < timeout:
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                response = await client.post(
                    "/graphql/", json={"query": "{ latestTick { index } }"}
                )
                if response.status_code == 200:
                    return
        except Exception:
            pass
        await asyncio.sleep(0.05)
    raise TimeoutError(f"Server on port {port} did not become ready within {timeout}s")


async def graphql_query(
    client: AsyncClient, query: str, variables: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Execute a GraphQL query and return the result."""
    payload: dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables
    response = await client.post("/graphql/", json=payload)
    assert response.status_code == 200
    return response.json()
