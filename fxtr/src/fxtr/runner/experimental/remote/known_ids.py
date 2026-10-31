"""The known-id tables: which entity ids each end of the wire lets the worker use, per attempt.

The worker may reference only what this attempt was given or stored. :class:`HostKnownIds`, on
the adapter's side, records every id the host gives the worker and every store the worker makes,
and checks each id the worker sends back against them. :class:`WorkerKnownIds`, on the proxy's
side, records the same sets as the worker sees them, so it answers ``is_known`` and refuses an
unknown reference before it crosses. Both tables are per attempt, created when the attempt is
assigned and dropped when its run ends, like node tokens; what one attempt knows means nothing
in another.

A store may be pipelined: the worker computes the stored value's cid itself and uses it at once,
before the host has stored it. The host registers the cid as pending when the store frame
arrives, and a reference to it that arrives meanwhile waits for the store to finish.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import anyio

from fxtr.core import codec
from fxtr.core.entity_data import EntityID, FxtrDataValue, StorableFxtrDataValue
from fxtr.entity_defns.array import Array
from fxtr.entity_defns.converter import DEFAULT_CONVERTER
from fxtr.entity_defns.dataclass_bases import ConversionError
from fxtr.runner.experimental.remote.wire import (
    ProtocolError,
    WireError,
    WireValue,
    decode_value,
    encode_value,
)


@dataclasses.dataclass(eq=False)
class _PendingStore:
    """A worker store the host has not finished: checks of its cid wait on ``done``."""

    done: anyio.Event = dataclasses.field(default_factory=anyio.Event)
    error: BaseException | None = None


class HostKnownIds:
    """One attempt's table on the host side: the cids given to the worker, and the worker's
    stores, pending or finished.

    A store goes through :meth:`expect` when its frame arrives and :meth:`complete` (or
    :meth:`fail`) when the host's store returns; a :meth:`check` of its cid in between waits. The
    worker may store the same value twice in an attempt; a second store of a pending cid joins
    the first.
    """

    def __init__(self) -> None:
        self._known: set[bytes] = set()
        self._pending: dict[bytes, _PendingStore] = {}

    # -- host to worker --

    def give(self, entity_id: EntityID) -> EntityID:
        """Record that the host gives ``entity_id`` to the worker, and return it."""
        self._known.add(entity_id.cid)
        return entity_id

    def give_value(self, value: StorableFxtrDataValue) -> WireValue:
        """A host value for the wire, every reference in it given to the worker.

        Raises:
            WireError: If the value is not encodable.
        """
        for ref in codec.iter_entity_ids(value):
            self.give(ref)
        return encode_value(value)

    def give_array(self, array: Array[Any]) -> WireValue:
        """A host array for the wire (its entity data value, references given)."""
        return self.give_value(DEFAULT_CONVERTER.unstructure_entity(array))

    # -- worker to host --

    def expect(self, cid: bytes) -> None:
        """A store frame arrived for ``cid``: until :meth:`complete` or :meth:`fail`, checks of it
        wait."""
        if cid not in self._known:
            self._pending.setdefault(cid, _PendingStore())

    def complete(self, claimed: bytes, stored: EntityID) -> None:
        """The host stored the value behind a store frame that claimed cid ``claimed``.

        Raises:
            ProtocolError: If the host stored a different cid (the worker hashed something other
                than what it sent).
        """
        if stored.cid != claimed:
            error = ProtocolError(
                f"The worker claimed cid {EntityID(claimed).cid_str()} but the host stored"
                f" {stored.cid_str()}"
            )
            self.fail(claimed, error)
            raise error
        self._known.add(claimed)
        pending = self._pending.pop(claimed, None)
        if pending is not None:
            pending.done.set()

    def fail(self, cid: bytes, error: BaseException) -> None:
        """The host refused the store of ``cid``: checks waiting on it, and any later, get
        ``error``."""
        pending = self._pending.get(cid)
        if pending is not None and not pending.done.is_set():
            pending.error = error
            pending.done.set()

    async def check(self, entity_id: EntityID) -> None:
        """Check that the worker may use ``entity_id``, waiting for a pending store of it.

        Raises:
            ProtocolError: If this attempt neither gave the worker the id nor had it stored.
            BaseException: The store's error, for a pending store the host refused.
        """
        if entity_id.cid in self._known:
            return
        pending = self._pending.get(entity_id.cid)
        if pending is None:
            raise ProtocolError(
                f"{entity_id!r} was neither given to nor stored by the worker in this attempt"
            )
        await pending.done.wait()
        if pending.error is not None:
            raise pending.error

    async def check_value(self, wire: WireValue) -> FxtrDataValue:
        """The value a frame from the worker carries, every reference in it checked.

        Raises:
            WireError: If the bytes are malformed.
            ProtocolError: As :meth:`check`.
        """
        value = decode_value(wire)
        for ref in codec.iter_entity_ids(value):
            await self.check(ref)
        return value

    async def check_array(self, wire: WireValue) -> Array[Any]:
        """The array a frame from the worker carries, every reference in it checked.

        Raises:
            WireError: If the value is not an array's data.
            ProtocolError: As :meth:`check`.
        """
        data = await self.check_value(wire)
        try:
            return DEFAULT_CONVERTER.structure(data, Array[Any])
        except ConversionError as exc:
            raise WireError(f"Value is not an array: {exc}") from exc

    @property
    def pending(self) -> int:
        """How many worker stores await their outcome."""
        return sum(1 for p in self._pending.values() if not p.done.is_set())


class WorkerKnownIds:
    """One attempt's table on the worker side: the cids the host has given this attempt and the
    cids it has stored.

    The proxy calls :meth:`note` for every id in a frame the host sent and :meth:`stored` for
    every value the worker stores; :meth:`is_known` is the answer to
    :meth:`~fxtr.runner.experimental.multiplexed.WorkerSession.is_known`, needing no message.
    """

    def __init__(self) -> None:
        self._known: set[bytes] = set()

    def note(self, entity_id: EntityID) -> None:
        """An id arrived from the host."""
        self._known.add(entity_id.cid)

    def note_value(self, wire: WireValue) -> None:
        """A value arrived from the host: remember every id in it.

        Raises:
            WireError: If the bytes are not canonical DAG-CBOR.
        """
        for entity_id in codec.iter_entity_ids(decode_value(wire)):
            self.note(entity_id)

    def stored(self, cid: bytes) -> EntityID:
        """This attempt is storing a value with cid ``cid``: the id the worker holds from now
        on."""
        self._known.add(cid)
        return EntityID(cid=cid)

    def is_known(self, entity_id: EntityID) -> bool:
        """Whether the worker may reference an id: one the host gave this attempt or one this
        attempt stored."""
        return entity_id.cid in self._known


__all__ = ["HostKnownIds", "WorkerKnownIds"]
