"""A fake attempt for testing the shared runs and the driver without a backend: it satisfies the
attempt-operations protocols of :mod:`fxtr.runner.assembly` over a :class:`Verifier` and an
in-memory store, and records every call."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from frozendict import frozendict

from fxtr.core.entity_data import EntityID
from fxtr.entity_defns.array import Array, ArrayType
from fxtr.runner import nodes
from fxtr.runner.assembly import VerificationPhase, Verifier
from fxtr.runner.nodes import Node, NodeIdentity
from fxtr.runner.session import AttemptClosedError, AttemptError, StepAssignment
from fxtr.storage.memory_storage import MemoryStorage


def identity(n: int) -> EntityID:
    """A stand-in identity for a node the host already has."""
    return EntityID(cid=b"\x01\x71\x12\x20" + (n + 1).to_bytes(32, "big"))


@dataclasses.dataclass(eq=False)
class FakeAttempt:
    """A ``WorkflowAttemptOps`` that records what a run asks of it.

    ``values`` answers ``observe`` by identity (``verify_observe`` serves the same values).
    ``calls`` is every recording or ending call in order, as ``(name, *args)`` tuples.
    """

    inputs: Mapping[str, tuple[NodeIdentity, ArrayType]] = frozendict()
    values: dict[NodeIdentity, Array[Any]] = dataclasses.field(default_factory=dict)
    workflow_name: str = "wf"
    pathname: str = "root"
    cache_key_prefix: str = "root"
    ended: bool = False
    entities: MemoryStorage = dataclasses.field(default_factory=MemoryStorage)
    verifier: Verifier = dataclasses.field(default_factory=Verifier)
    calls: list[tuple[Any, ...]] = dataclasses.field(default_factory=list)

    # -- primary --

    def emit(self, node: Node) -> None:
        self.verifier.note_emit(node.content_id())
        self.calls.append(("emit", node))

    def emit_source_array(self, node: nodes.SourceArray, array: Array[Any]) -> None:
        self.entities.store_sync(array)
        self.emit(node)

    def log(self, message: str) -> None:
        self.calls.append(("log", message))

    async def observe(self, identity: NodeIdentity) -> Array[Any]:
        self.verifier.note_observe(identity)
        self.calls.append(("observe", identity))
        return self.values[identity]

    async def record_return(self, identity: NodeIdentity) -> None:
        self.calls.append(("record_return", identity))

    async def hold_return(self, identity: NodeIdentity) -> None:
        self.verifier.note_return(identity)
        self.calls.append(("hold_return", identity))

    # -- verification --

    def _require_verifying(self) -> None:
        if self.ended:
            raise AttemptClosedError("closed")
        if self.verifier.phase != "verifying":
            raise AttemptClosedError(f"not verifying (phase {self.verifier.phase})")

    def verify_emit(self, node: Node) -> None:
        self._require_verifying()
        self.verifier.check_emit(node.content_id(), nodes.kind_of(node))

    def verify_log(self, message: str) -> None:
        self._require_verifying()

    def verify_observe(self, identity: NodeIdentity) -> Array[Any]:
        self._require_verifying()
        self.verifier.check_observe(identity)
        return self.values[identity]

    def verify_return(self, identity: NodeIdentity) -> NodeIdentity:
        self._require_verifying()
        return self.verifier.check_return(identity)

    def verification_failed(self) -> None:
        self.verifier.mark_failed()

    @property
    def verification_phase(self) -> VerificationPhase:
        return self.verifier.phase

    # -- ending --

    def fail(self, error: AttemptError, *, cancel_body: bool) -> None:
        if self.ended:
            return
        self.ended = True
        self.calls.append(("fail", error.type_name, cancel_body))

    def abandon(self, reason: str) -> None:
        if self.ended:
            return
        self.ended = True
        self.calls.append(("abandon", reason))

    async def flush(self) -> None:
        self.calls.append(("flush",))


@dataclasses.dataclass(eq=False)
class FakeStepAttempt:
    """A ``StepAttemptOps`` that records what a run asks of it."""

    assignment: StepAssignment
    ended: bool = False
    entities: MemoryStorage = dataclasses.field(default_factory=MemoryStorage)
    calls: list[tuple[Any, ...]] = dataclasses.field(default_factory=list)

    def add_log(self, message: str) -> None:
        self.calls.append(("log", message))

    async def complete(self, result: Array[Any]) -> None:
        self.ended = True
        self.calls.append(("complete", result))

    def fail(self, error: AttemptError) -> None:
        if not self.ended:
            self.ended = True
            self.calls.append(("fail", error.type_name))

    def abandon(self, reason: str) -> None:
        if not self.ended:
            self.ended = True
            self.calls.append(("abandon", reason))
