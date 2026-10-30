"""Host-side demo DATA entity for ``basic_test_demo``.

The agent_workdir flake materializes :data:`DEMO_DATA` as the nix DATA
entity ``fxtr.entities.demo_data`` via ``mkEntityFromPython`` (which runs
this module in a host venv and pins the resolved entity into a layered
store). A ``run_script`` then pulls it in with
``retrieve_nix_entity("demo_data")``.

``DemoDataEntity`` is a small, **self-contained** entity type (its own
namespace + ``define_type``) — deliberately not reusing ``demo_pkg.Note``,
because ``demo_pkg`` is a sandbox-oriented package whose import chain pulls
in the sandbox-only bridge and so can't be imported from a host venv. The
entity is stored into ``GLOBAL_SCOPE`` at import because
``mk_entity_from_python_writer`` pins from there.
"""

from __future__ import annotations

import dataclasses
import uuid

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE

#: Stable namespace UUID for this package's demo entity type.
_DEMO_DATA_NS = uuid.UUID("a7c1e4d2-9b63-4f08-8e15-6d2a3c4b5f90")
DEMO_DATA_TYPE_ID: FxtrID = system_entities.define_type(_DEMO_DATA_NS, "demo_data")


@dataclasses.dataclass(frozen=True)
class DemoDataEntity(DataclassEntity, fxtr_type=DEMO_DATA_TYPE_ID):
    """A tiny standalone DATA entity exercised by ``retrieve_nix_entity``."""

    title: str
    body: str


#: The entity object ``mkEntityFromPython`` resolves via
#: ``fromPython = "demo_data:DEMO_DATA"``. Stored (not just constructed) so
#: it's an ``EntityRef`` reachable from ``GLOBAL_SCOPE``.
DEMO_DATA = DemoDataEntity(
    title="demo data",
    body="Built by nix as fxtr.entities.demo_data; pulled in via retrieve_nix_entity.",
).store(GLOBAL_SCOPE)
