"""Network transport handles: capability tokens for network access from steps."""

from __future__ import annotations

import abc
from typing import Any

import anyio.abc

from fxtr2.data.entity_core import FxtrID


class NetworkTransportHandle(abc.ABC):
    """Handle providing network transport capability to steps.

    Steps receive these as arguments and use connect() to open byte streams.
    The handle's identity is included in cache keys — swapping a different
    transport changes the cache key.
    """

    @property
    @abc.abstractmethod
    def identity(self) -> FxtrID:
        """The identity of this transport, used for cache key computation."""
        ...

    @abc.abstractmethod
    async def connect(self) -> anyio.abc.ByteStream:
        """Open a new connection through this transport.

        Returns a ByteStream that the caller can read/write. The transport
        controls where the connection goes and what security policies apply.
        """
        ...


class ExternalNetworkTransportHandle(NetworkTransportHandle):
    """A transport handle wrapping a caller-provided ByteStreamConnectable.

    The key_id is an opaque FxtrID chosen by the caller to identify what
    this transport represents (e.g., which API service it connects to).
    """

    def __init__(self, connector: Any, key_id: FxtrID) -> None:
        self._connector = connector
        self._key_id = key_id

    @property
    def identity(self) -> FxtrID:
        return self._key_id

    async def connect(self) -> anyio.abc.ByteStream:
        return await self._connector.connect()
