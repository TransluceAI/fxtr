"""Cap-bridge taxonomy: four variants on Local/Pipelined × Independent/Dependent.

A *cap-bridge* is the lease-minting machinery a holder stores at-rest
to produce fresh leases on a token whose lifetime is independent of
the holder's. Two orthogonal axes:

- **Independent vs Dependent.** Independent bridges store a single
  pre-existing lease (concrete or pipelined) and clone it on each
  lookup. Dependent bridges store a dict of input leases plus a
  converter callable that mints a fresh output lease per call from
  ``(cloned_inputs, base_lease)`` — used when persistently holding
  a ``Lease[handle.token]`` would create a cycle (per
  ``plans/phase3/stage8c_user_caps/lifetime_conversion_mechanics.md``).
- **Local vs Pipelined.** Local bridges produce concrete ``Lease``
  values; pipelined bridges produce :class:`UnwrapForPipelining`
  ``(future, lease)`` pairs that downstream code can chain into a
  Cap-IR dispatch without a synchronous round-trip.

The four resulting variants — :class:`IndependentLocalBridge`,
:class:`IndependentPipelinedBridge`, :class:`DependentLocalBridge`,
:class:`DependentPipelinedBridge` — make the Local/Pipelined axis
*static*, so consumers that need to discriminate (e.g. the wire
return-resources path, or future bridge integrations restricted to
the Local cases) can dispatch on type rather than runtime-check at
every use site.

Each bridge dataclass exposes two methods:

- ``clone() -> Self`` — produce a fresh independent at-rest copy.
  Stored leases are cloned; converter callables are shared by
  reference (they capture only immutable state). For pipelined
  wrappers the inner future is shared and the inner lease cloned —
  same semantics as ``UnwrapForPipelining``'s "share future,
  reference-count via lease" contract.
- ``discard_at_rest() -> None`` — discard every stored lease.
  Idempotent: ``Lease.discard()`` is itself idempotent and
  ``Dependent*`` variants clear their ``leases`` dict so repeat
  calls are no-ops.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable

from .lease_types import Lease, LifetimeToken
from .leased_cap_ir import UnwrapForPipelining

type DependentLocalBridgeConverter = Callable[
    [dict[LifetimeToken[Any], Lease[Any]], Lease[Any]],
    Lease[Any],
]
"""``(input_leases, base) -> Lease`` — sync converter producing a concrete lease.

Receives a freshly-cloned dict of stored at-rest leases (one entry
per stored token, keyed by subject token) and a freshly-cloned
``base`` lease. Owns its inputs: every dict entry and ``base``
must be consumed (returned, threaded into a deeper call, or
discarded). Returns a fresh ``Lease[target]``.
"""


type DependentPipelinedBridgeConverter = Callable[
    [
        dict[LifetimeToken[Any], Lease[Any] | UnwrapForPipelining[Lease[Any], Any]],
        Lease[Any],
    ],
    UnwrapForPipelining[Lease[Any], Any],
]
"""``(input_leases, base) -> UnwrapForPipelining[Lease, …]`` — pipelined converter.

Same ownership rules as :data:`DependentLocalBridgeConverter`, but
input dict entries may themselves be pipelined (chain bridges merge
across multiple input bridges, some of which may have been pipelined),
and the return is a ``(future, lease)`` pair so downstream code can
chain a wire dispatch without awaiting locally. Typical use: a chain
converter that dispatches a wire ``IRHandle.dispatch`` and forwards
the resulting future.
"""


def _clone_pipelined_lease_value(
    value: Lease[Any] | UnwrapForPipelining[Lease[Any], Any],
) -> Lease[Any] | UnwrapForPipelining[Lease[Any], Any]:
    """Clone a possibly-pipelined at-rest lease value.

    For a concrete :class:`Lease`, returns ``lease.clone()``. For an
    :class:`UnwrapForPipelining`, returns a new wrapper that shares
    the underlying future and clones the inner keep-alive lease — the
    shared future is a stable reference, and cloning the inner lease
    tracks an additional consumer.
    """
    if isinstance(value, UnwrapForPipelining):
        return UnwrapForPipelining(future=value.future, lease=value.lease.clone())
    return value.clone()


def _discard_pipelined_lease_value(
    value: Lease[Any] | UnwrapForPipelining[Lease[Any], Any],
) -> None:
    """Discard a possibly-pipelined at-rest lease value.

    For an :class:`UnwrapForPipelining`, discards the wrapper's inner
    keep-alive lease; the shared future is then unreferenced from
    this holder and gets cleaned up via the normal Cap-IR path. For
    a concrete :class:`Lease`, just calls ``lease.discard()``.
    """
    if isinstance(value, UnwrapForPipelining):
        value.lease.discard()
    else:
        value.discard()


@dataclasses.dataclass
class IndependentLocalBridge:
    """Concrete-lease bridge: stores a ``Lease`` independent of the holder's lifetime.

    Each lookup clones the stored lease via :meth:`clone`. Used when
    the cap was already alive before the holder existed (e.g. an
    arg-shaped lease the caller passed in), so persistently holding
    it does not introduce a cycle. The "Local" half of the taxonomy:
    output is always a concrete ``Lease``.
    """

    lease: Lease[Any]

    def clone(self) -> "IndependentLocalBridge":
        return IndependentLocalBridge(lease=self.lease.clone())

    def discard_at_rest(self) -> None:
        self.lease.discard()


@dataclasses.dataclass
class IndependentPipelinedBridge:
    """Pipelined-lease bridge: stores an ``UnwrapForPipelining[Lease, …]``.

    The pipelined wrapper is a frozen ``(future, lease)`` pair whose
    inner lease keeps the shared future alive. Each lookup shares the
    future and clones the inner lease (see
    :func:`_clone_pipelined_lease_value`). Arises when a lift walked
    into a parent ``DependentPipelinedBridge`` whose converter
    produced an ``UnwrapForPipelining`` — we store the wrapper as-is
    rather than awaiting eagerly, preserving the "ride through the
    next dispatch" property on first use.
    """

    pipelined: UnwrapForPipelining[Lease[Any], Any]

    def clone(self) -> "IndependentPipelinedBridge":
        return IndependentPipelinedBridge(
            pipelined=UnwrapForPipelining(
                future=self.pipelined.future,
                lease=self.pipelined.lease.clone(),
            )
        )

    def discard_at_rest(self) -> None:
        self.pipelined.lease.discard()


@dataclasses.dataclass
class DependentLocalBridge:
    """Dependent bridge that produces concrete ``Lease`` values.

    Stores a dict of concrete input leases plus a converter that
    consumes those leases plus a ``base`` lease and returns a fresh
    output ``Lease``. Used when persistently storing a
    ``Lease[handle.token]`` would create a cycle, but the conversion
    itself is purely local (no wire dispatch). Canonical example:
    case-3 ``derived.exchange_reverse`` adapter, or the trivial
    identity bridge installed by ``mount_lowlevel_cap_as_service``.

    The "Local" axis is enforced statically on both inputs and
    output — the inputs dict carries concrete leases only.
    """

    leases: dict[LifetimeToken[Any], Lease[Any]]
    converter: DependentLocalBridgeConverter

    def clone(self) -> "DependentLocalBridge":
        return DependentLocalBridge(
            leases={tok: lease.clone() for tok, lease in self.leases.items()},
            converter=self.converter,
        )

    def discard_at_rest(self) -> None:
        for lease in self.leases.values():
            lease.discard()
        self.leases.clear()


@dataclasses.dataclass
class DependentPipelinedBridge:
    """Dependent bridge that produces ``UnwrapForPipelining[Lease, …]`` values.

    Stores a dict of possibly-pipelined input leases plus a converter
    that consumes those leases plus a ``base`` lease and returns a
    pipelined ``(future, lease)`` pair. Used by chain converters that
    dispatch a wire ``IRHandle`` and forward the resulting future
    without awaiting locally.

    The input dict permits pipelined entries because chain bridges
    merge inputs across multiple sources (some of which may have
    been ``IndependentPipelinedBridge`` or ``DependentPipelinedBridge``);
    the converter is responsible for cloning / forwarding each as
    appropriate.
    """

    leases: dict[LifetimeToken[Any], Lease[Any] | UnwrapForPipelining[Lease[Any], Any]]
    converter: DependentPipelinedBridgeConverter

    def clone(self) -> "DependentPipelinedBridge":
        return DependentPipelinedBridge(
            leases={tok: _clone_pipelined_lease_value(v) for tok, v in self.leases.items()},
            converter=self.converter,
        )

    def discard_at_rest(self) -> None:
        for v in self.leases.values():
            _discard_pipelined_lease_value(v)
        self.leases.clear()


type LocalBridge = IndependentLocalBridge | DependentLocalBridge
"""Bridge variants whose output is always a concrete ``Lease``.

Used for contexts that statically rule out pipelining — typically
ancestor / parent bridges (where the lift path needs synchronous
``Lease[parent.main_token]`` minting) and the future
MemoryStore-bridge integration restricted to the local cases.
"""


type LocalOrPipelinedBridge = (
    IndependentLocalBridge
    | DependentLocalBridge
    | IndependentPipelinedBridge
    | DependentPipelinedBridge
)
"""Any of the four bridge variants — the canonical "cap-bridge" sum."""


__all__ = [
    "DependentLocalBridge",
    "DependentLocalBridgeConverter",
    "DependentPipelinedBridge",
    "DependentPipelinedBridgeConverter",
    "IndependentLocalBridge",
    "IndependentPipelinedBridge",
    "LocalBridge",
    "LocalOrPipelinedBridge",
]
