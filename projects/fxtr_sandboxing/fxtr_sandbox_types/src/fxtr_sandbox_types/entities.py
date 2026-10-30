"""Entity types shared between the host and the sandbox.

Used by the host's spawner pipeline and by the sandbox-side runtime.
Content-addressed via mashumaro.

The deprecated in-band-bytes *bundle* entity types (the WASM ``TaskBundle``
+ its parts) live in ``deprecated_entities.py`` (still re-exported from the
package root). This module holds the live git-ref spawn references + the
sandbox-provenance entities.
"""

import dataclasses

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity, EntityRef
from fxtr2.conversion.converters.unknown_entity import UnknownEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FrozenFxtrCbor, FxtrID

from ._namespace import SHARED_SANDBOX_NS


@dataclasses.dataclass(frozen=True)
class NixWasmServiceDefn(
    DataclassEntity,
    fxtr_type=system_entities.define_type(SHARED_SANDBOX_NS, "nix_wasm_service_defn"),
):
    """A *reference* to a sandbox service: code to build, not carried code.

    Unlike the deprecated ``TaskBundle`` (which inlines the resolved source),
    this points at a ``fxtr.services.<name>`` attr in a flake pinned by git
    commit. The spawner resolves it via a nix git flake ref
    (``git+file://<repo>?rev=<git_commit>&dir=<flake_path>#<flake_attr>``)
    and builds the code itself — see ``fxtr_sandboxing.nix`` and
    ``projects/fxtr_workstation/plans/phase0/part5_nix_workdir/design_notes/12_nix_drv_reference.md``.

    ``entry_point`` is **not** here — it lives on the flake attr
    (``<flake_attr>.entryPoint``) and the spawner reads it via ``nix eval``,
    so the entry point is versioned with the code.

    The agent constructs this in-script and ``.store()``s it, then passes it
    to the unchanged ``spawn``. A wire-identical duplicate lives in the
    sandbox bridge so the agent can import it; same ``define_type`` + fields
    → same wire type.
    """

    # The commit pinning the whole flake (source + its committed flake.lock).
    git_commit: str

    # The service attr path, e.g. "fxtr.services.my_service".
    flake_attr: str

    # Repo-subpath of the flake (``&dir=``); None = repo root.
    flake_path: str | None = None


@dataclasses.dataclass(frozen=True)
class NixContainerServiceDefn(
    DataclassEntity,
    fxtr_type=system_entities.define_type(SHARED_SANDBOX_NS, "nix_container_service_defn"),
):
    """A *reference* to a container-kind sandbox service — the OCI sibling
    of :class:`NixWasmServiceDefn`, with the identical field shape.

    Points at a ``fxtr.services.<name>`` attr whose buildable leaf is a
    nix2container ``image`` (a JSON manifest referencing store paths as
    layers) instead of a wasm ``sitePackages``. The spawner resolves it via
    the same git flake ref, checks the attr's ``kind`` (``"container"``),
    builds the image manifest, and delivers it into the container runtime
    via the nix2container-patched skopeo. Unlike the wasm attr there is no
    ``entryPoint`` leaf — the entry point is baked into the image's ``Cmd``.

    See ``fxtr_sandboxing.nix`` / ``fxtr_sandboxing.nix_container_runner``
    and
    ``projects/fxtr_workstation/plans/phase0/part5_nix_workdir/stage7_nix_container.md``.

    The agent constructs this in-script and ``.store()``s it, then passes it
    to the unchanged ``spawn``. A wire-identical duplicate lives in the
    sandbox bridge so the agent can import it; same ``define_type`` + fields
    → same wire type.
    """

    # The commit pinning the whole flake (source + its committed flake.lock).
    git_commit: str

    # The service attr path, e.g. "fxtr.services.my_service".
    flake_attr: str

    # Repo-subpath of the flake (``&dir=``); None = repo root.
    flake_path: str | None = None


@dataclasses.dataclass(frozen=True)
class NixPyodideServiceDefn(
    DataclassEntity,
    fxtr_type=system_entities.define_type(SHARED_SANDBOX_NS, "nix_pyodide_service_defn"),
):
    """A *reference* to a pyodide-kind sandbox service — the
    pyodide-in-deno sibling of :class:`NixWasmServiceDefn`, with the
    identical field shape.

    Points at a ``fxtr.services.<name>`` attr built by ``mkPyodideService``
    (``kind = "pyodide"``): a ``sitePackages`` leaf that may include PEP 783
    ``pyemscripten_*_wasm32`` binary wheels, a ``runtimeBundle`` leaf
    (pyodide assets + guest bootstrap), and an ``entryPoint``. The spawner
    resolves it via the same git flake ref, checks the attr's ``kind``,
    builds + materializes both leaves, and runs the guest via the
    host-trusted deno driver. See ``fxtr_sandboxing.pyodide_runner`` and
    ``projects/fxtr_sandboxing/plans/stage2/2p5_deno_py_sandbox/plan.md``.

    The agent constructs this in-script and ``.store()``s it, then passes it
    to the unchanged ``spawn``. A wire-identical duplicate lives in the
    sandbox bridge so the agent can import it; same ``define_type`` + fields
    → same wire type.
    """

    # The commit pinning the whole flake (source + its committed flake.lock).
    git_commit: str

    # The service attr path, e.g. "fxtr.services.my_service".
    flake_attr: str

    # Repo-subpath of the flake (``&dir=``); None = repo root.
    flake_path: str | None = None


@dataclasses.dataclass(frozen=True)
class NixServiceDefn(
    DataclassEntity,
    fxtr_type=system_entities.define_type(SHARED_SANDBOX_NS, "nix_service_defn"),
):
    """A *kind-agnostic* reference to a sandbox service: the same
    ``(git_commit, flake_attr, flake_path)`` triple as
    :class:`NixWasmServiceDefn` / :class:`NixContainerServiceDefn` /
    :class:`NixPyodideServiceDefn`, without committing to a runtime.

    The spawner's auto-dispatch contract: on spawn it evals the attr's
    ``kind`` discriminator and runs the matching runtime arm — "spawn
    whatever kind the attr declares". Use this when the caller holds
    only the reference triple (e.g. a stored binding record) and the
    flake attr is the source of truth for its own kind; use a concrete
    defn type to *assert* a kind (a mismatch then errors instead of
    silently following the attr).

    Spawn provenance records this entity as the bundle, so an
    auto-dispatched spawn is identifiable as such.
    """

    # The commit pinning the whole flake (source + its committed flake.lock).
    git_commit: str

    # The service attr path, e.g. "fxtr.services.my_service".
    flake_attr: str

    # Repo-subpath of the flake (``&dir=``); None = repo root.
    flake_path: str | None = None


@dataclasses.dataclass(frozen=True)
class DirectLauncherSentinel(
    DataclassEntity,
    fxtr_type=system_entities.define_type(SHARED_SANDBOX_NS, "direct_launcher_sentinel"),
):
    """Placeholder bundle entity for direct-launcher provenance.

    ``direct_launcher`` doesn't have a real bundle — it runs from a
    caller-supplied eryx ``SandboxFactory`` + an entry-point string.
    The guest's provenance pipeline still expects a bundle ref on
    every ``connect`` call, so direct_launcher threads in a
    content-addressed instance of this entity instead. The resulting
    c-node's ``bundle`` field then identifies the spawn as
    "direct-launcher" rather than pointing at a real bundle.

    Contains no fields — every direct-launcher spawn shares the same
    entity id under content-addressing, so the provenance marker is
    stable across invocations.
    """


SERVICE_CONFIG_REPLACEMENT_TYPE_ID = system_entities.define_type(
    SHARED_SANDBOX_NS, "service_config_replacement"
)
"""Type id for :class:`ServiceConfigReplacementEntity`. Exposed at module
top-level so the guest-side walker can compare against the loaded type
of any user-provided EntityRef without instantiating the class."""


@dataclasses.dataclass(frozen=True)
class ServiceConfigReplacementEntity(
    DataclassEntity,
    fxtr_type=SERVICE_CONFIG_REPLACEMENT_TYPE_ID,
):
    """Leaf substitute for a :class:`ServiceRef` in the sandbox-guest
    provenance snapshot.

    When the guest builds its per-``connect`` provenance context, it
    walks the user's ``FxtrServiceData`` config and replaces every
    :class:`ServiceRef` leaf with one of these — capturing the service's
    runtime type identity and its mount-scope context marker. The two
    fields propagate provenance across the sandbox boundary: a service
    bridged in by the caller carries its own ``$fxtr.ctx`` (often
    layered with the caller's own provenance c-node), and that chain
    flows into the guest's c-node payload via the provenance-aware
    ``store_cbor`` path.

    Always emitted as a stored entity with its own ``$fxtr.type``
    discriminator (not a :class:`DataclassFxtrSubpart`) so the walker
    can substitute it at any depth in the config tree.
    """

    service_type_id: FxtrID
    """The service's ``$fxtr.type`` — the FxtrID identifying the service's
    runtime type. Reachable on the wire via the reserved
    ``"$fxtr.type"`` framework method."""

    service_ctx: FxtrID
    """The service's ``$fxtr.ctx`` — the mount scope's context marker.
    Reachable on the wire via the reserved ``"$fxtr.ctx"`` framework
    method. Typically a ``"."`` ground node, possibly with a
    ``_provenance_parent`` layered on if the service was mounted in a
    provenanced scope."""


@dataclasses.dataclass(frozen=True)
class SandboxProvenanceContext(
    DataclassEntity,
    fxtr_type=system_entities.define_type(SHARED_SANDBOX_NS, "sandbox_provenance_context"),
):
    """d-node payload for the per-``connect`` provenance c-node.

    The guest-side runtime (``fxtr_sandbox_rpc_adapter``) builds one of
    these on every ``connect_with_bundle_provenance`` call, hands its
    cbor to :meth:`FxtrScope.create_c_node`, then sets the resulting
    c-node as the per-call dispatch scope's provenance context. From
    that point on, every entity / service produced inside the guest
    carries this c-node as its ``_provenance_parent`` chain anchor.

    The ``value`` field defaults to ``FxtrID.builtin(".")`` so the
    minted c-node unwraps cleanly to the ground node, as required by
    the c-node validation in the memory arena.
    """

    bundle: EntityRef[UnknownEntity]
    """The bundle entity (``NixWasmServiceDefn`` /
    ``NixContainerServiceDefn`` / the deprecated ``TaskBundle`` /
    ``DirectLauncherSentinel``) that spawned this guest. Threaded in by
    the host-side spawn wrapper, *outside* the user-controlled config,
    so user code can't forge it. Typed as ``EntityRef[UnknownEntity]``
    because the field is provenance metadata — we only need the ID,
    not typed Python access to the loaded entity."""

    config_snapshot: FrozenFxtrCbor
    """Immutable snapshot of the user's ``connect`` config arg. Same
    shape as the input ``FxtrServiceData`` tree, except every
    ``ServiceRef`` leaf has been replaced by an EntityRef-shaped
    pointer to a :class:`ServiceConfigReplacementEntity`. Captured at
    walk time; FxtrIDs ride through as ``RawWeakFxtrID`` /
    ``RawStrongFxtrID`` leaves."""

    value: FxtrID = dataclasses.field(default_factory=lambda: FxtrID.from_tag_and_payload(".", b""))
    """C-node ``unwraps_to`` target. Always ``.`` for the sandbox
    provenance shape — the c-node unwraps to the ground node, matching
    how :meth:`FxtrScope.provenance_context` consumes it."""
