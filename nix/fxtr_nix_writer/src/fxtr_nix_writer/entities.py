"""Entity types written by fxtr_nix_writer's writers.

Lib-internal types (not user-facing). Each is a normal fxtr2
``DataclassEntity`` with a stable ``define_type`` id, so a fxtr2 reader on
the other side can deserialize it by importing the same class.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data.system_entities import define_type

# Stable namespace for fxtr_nix_writer's own entity types.
FXTR_NIX_WRITER_NS = uuid.UUID("6f1c8a42-3d7e-4b90-9a1f-2c5d8e0b7a36")


@dataclass(frozen=True)
class HelloWorldEntity(
    DataclassEntity,
    fxtr_type=define_type(FXTR_NIX_WRITER_NS, "hello_world"),
):
    """Trivial entity used to exercise the writer scaffold end-to-end."""

    message: str
