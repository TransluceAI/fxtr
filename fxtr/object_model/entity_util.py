"""Utility functions for working with entities."""

from __future__ import annotations

import dataclasses

import cbrrr

from fxtr.object_model import core


@dataclasses.dataclass
class EntityAccessPath:
    """Access path where an entity is accessible, relative to some root object."""

    entity: core.EntityBase
    py_path: core.AccessPath
    ipld_path: core.AccessPath | None


class EntityChildCollector(core.RecursiveEntityVisitor):
    """Visitor for finding all entities accessible from a hub object with a known spec."""

    def __init__(self):
        super().__init__()
        self.visited_entities: list[EntityAccessPath] = []

    def visit_entity(
        self,
        *,
        py_path_from_root: core.AccessPath,
        ipld_path_from_root: core.AccessPath,
        entity: core.DataEntityBase,
    ):
        access_path = EntityAccessPath(
            entity=entity, py_path=py_path_from_root, ipld_path=ipld_path_from_root
        )
        self.visited_entities.append(access_path)


def walk_ipld_path(value: cbrrr.DagCborTypes, ipld_path: core.AccessPath) -> cbrrr.DagCborTypes:
    """Walk the IPLD path and return the value at the end.

    Args:
        value: The value to walk.
        ipld_path: The IPLD path to walk.

    Returns:
        The value at the end of the IPLD path.
    """
    for step in ipld_path:
        match step:
            case core.AccessPathStep_GetItem_Str(key=key):
                assert isinstance(value, dict)
                value = value[key]
            case core.AccessPathStep_GetItem_Int(key=key):
                assert isinstance(value, list)
                value = value[key]
            case _:
                raise ValueError(f"Invalid IPLD path step: {step}")
    return value


@dataclasses.dataclass(frozen=True)
class PathBuilder:
    """Builder for IPLD paths."""

    path: core.AccessPath = ()

    def build(self) -> core.AccessPath:
        return self.path

    def __getitem__(self, key: str | int) -> PathBuilder:
        if isinstance(key, str):
            return PathBuilder(self.path + (core.AccessPathStep_GetItem_Str(key=key),))
        elif isinstance(key, int):
            return PathBuilder(self.path + (core.AccessPathStep_GetItem_Int(key=key),))
        else:
            raise ValueError(f"Invalid key type: {type(key)}")


PATH_BUILDER = PathBuilder()
