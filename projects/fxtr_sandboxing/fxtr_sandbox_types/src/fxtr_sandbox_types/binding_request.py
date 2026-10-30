"""The standard connect-request shape for bound workflow services.

`fxtr_experiment` (the durable workflow system) serves *handle keys*
through bindings: a handle cell holds a key entity (the service's
fingerprint identity), and a runner-level binding says what actually
serves that key — either a registrant-supplied live connector service
or a sandboxed service definition the runner spawns on demand. Both
classes deliver **one request shape** to the bound implementation:
:class:`BindingConnectRequest`, defined here so that host-side
connectors and sandbox guest entrypoints implement against the same
interface (an implementation moves between the two mechanically). See
``projects/fxtr_experiment/notes/sandboxed_bindings.md`` for the whole
binding design.

This module is deliberately fxtr_experiment-free: it is guest-importable
under both fxtr2 flavor extras, like everything else in this package.
"""

import dataclasses
from typing import Mapping, Sequence

from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import ServiceRef
from fxtr2.services.service_cap_common import UnknownService

type JSONValue = (None | bool | int | float | str | Mapping[str, JSONValue] | Sequence[JSONValue])
"""A plain JSON value — the binding-config vocabulary (no entities, no refs).

Codec notes, both load-bearing for the typed-service codec (mashumaro):
the recursion is *bare* (PEP 695 aliases evaluate lazily; a quoted
``"JSONValue"`` becomes a forward ref the codec builder cannot resolve
from its own module context), and the member order matters for
round-trip fidelity through union decoding, which tries members left to
right — ``bool`` before ``int`` (bool subclasses int), ``str`` before
the collections (str is a Sequence), and ``Mapping`` before
``Sequence`` (iterating a dict as a sequence would decode it to its key
list). Distinct from ``json_object_stream.JSONTypes`` (the stream-frame
vocabulary), whose member order predates this constraint and which is
never codec-decoded against.
"""


@dataclasses.dataclass(frozen=True)
class BindingConnectRequest:
    """What a bound service implementation receives per spawn.

    The workflow runner assembles one request per binding spawn and
    delivers it to whatever serves the binding: an external connector
    receives it as the argument of ``connect(request)``; a sandboxed
    service entrypoint receives it as its connect config. Plain
    dataclass (wire config, not stored data) — every field crosses
    service boundaries via the standard typed-service codec.

    Attributes:
        key: The handle key entity's id — the fingerprint identity this
            spawn is serving (the value stored in the handle cell, which
            task caching identity depends on). Identity-bearing service
            arguments belong in this entity. The runner guarantees the
            key's closure is covered by the call's arena, so the
            implementation may load it in the dispatch scope.
        config: The binding's optional config blob — machine-local,
            non-identity tuning (concurrency limits, timeouts, base
            URLs). Plain JSON values only, by contract and by type.
        handles: The binding's pinned dependencies — name → a
            pre-spawned service for each handle the binding declared it
            needs, resolved and pin-checked by the runner at spawn time.
    """

    key: FxtrID
    config: JSONValue = None
    handles: Mapping[str, ServiceRef[UnknownService]] = dataclasses.field(default_factory=dict)
