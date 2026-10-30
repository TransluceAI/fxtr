"""Deprecated in-band-bytes sandbox-bundle entity types (WASM only).

These carry resolved code *as bytes* (source snapshots, wheel refs) — the
model superseded by the git-referenced
:class:`~fxtr_sandbox_types.entities.NixWasmServiceDefn` (code built by the
spawner from a git commit). Kept (and still re-exported from the package
root, so ``from fxtr_sandbox_types import TaskBundle`` keeps working) for
the deprecated ``prepare`` path + the existing ``_spawn_runner_wasm`` arm.
The container siblings (``DockerTaskBundle``, ``PyContainerTaskBundle`` +
their parts) were removed in
``projects/fxtr_workstation/plans/phase0/part5_nix_workdir/stage8_podman.md``
— their live replacement is
:class:`~fxtr_sandbox_types.entities.NixContainerServiceDefn`. The
``define_type`` ids are unchanged from when these lived in ``entities.py``
— same namespace + labels → same wire types.

Location is cosmetic; see
``projects/fxtr_workstation/plans/phase0/part5_nix_workdir/design_notes/12_nix_drv_reference.md``.
"""

import dataclasses

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity, EntityRef
from fxtr2.data import system_entities

from ._namespace import SHARED_SANDBOX_NS


@dataclasses.dataclass(frozen=True)
class PySourcePackage(
    DataclassEntity,
    fxtr_type=system_entities.define_type(SHARED_SANDBOX_NS, "py_source_package"),
):
    """A content-addressed snapshot of a Python package's distributed source.

    Built by building the package's wheel, unpacking it, and extracting
    all files except .dist-info/. Keys are relative paths within site-packages.
    """

    source_files: dict[str, str]


@dataclasses.dataclass(frozen=True)
class PyWheelPackage(
    DataclassEntity,
    fxtr_type=system_entities.define_type(SHARED_SANDBOX_NS, "py_wheel_package"),
):
    """Metadata for a remotely-published pure-Python wheel.

    Contains enough information to fetch and verify the wheel at runtime.
    """

    name: str
    version: str
    index: str
    wheel_url: str
    wheel_sha256: str


@dataclasses.dataclass(frozen=True)
class TaskBundle(
    DataclassEntity,
    fxtr_type=system_entities.define_type(SHARED_SANDBOX_NS, "task_bundle"),
):
    """A fully-resolved, content-addressed WASM task bundle.

    ``entry_point`` is the ``module:function`` string taken from the task's
    ``[project.entry-points."fxtr.service"]`` table at prepare time. The host
    runner injects it into the sandbox; ``fxtr_sandbox_rpc_adapter.main`` imports
    that callable — it does not scan ``importlib.metadata`` at runtime.
    """

    # Package name -> reference to either a source package or wheel package
    packages: dict[str, EntityRef[PySourcePackage | PyWheelPackage]]

    # Python version constraint from pyproject.toml, e.g. ">=3.12"
    requires_python: str

    # e.g. "my_task:run" — validated during prepare; pinned on the bundle
    entry_point: str
