"""Round-trip test over the write_hello_world layer: nix-build the layered
store, read the HelloWorldEntity back with a fxtr2 reader, check its message.

The `nix_build` / `flake_ref` fixtures (in conftest.py) build via a git
flake reference to the repo root, so run from the colocated jj↔git worktree.
"""

from __future__ import annotations

from collections.abc import Callable

import anyio
import pytest

from fxtr2.data.layered_file_store.reader import LayeredFileStoreReader
from fxtr_nix_writer.entities import HelloWorldEntity


@pytest.fixture(scope="module")
def hello_world_layer(nix_build: Callable[[str], str]) -> str:
    return nix_build("packages.x86_64-linux.hello-world-layer")


def test_hello_world_round_trip(hello_world_layer: str) -> None:
    async def _read() -> None:
        async with LayeredFileStoreReader(hello_world_layer) as reader:
            ref = await reader.retrieve("hello", HelloWorldEntity)
            entity = await ref.load()
            assert entity.message == "hello world"

    anyio.run(_read)
