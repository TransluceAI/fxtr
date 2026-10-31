"""Versioned fxtr type names for the behaviors package.

Type evolution
--------------
fxtr decodes strictly: every declared field must be present and no extra keys
are allowed. Bump a type's version suffix when its fields change in a way
that leaves previously serialized data undecodable: adding a field, even
with a default; removing or renaming a field; or changing a field's type
incompatibly (``Usage.v1`` -> ``Usage.v2``). New types start at ``.v1``.
Widening a union or changing methods or defaults without changing the stored
shape does not require a bump.

Each version identifies a stored format. Keep the current tags when
publishing, and assign a new version to any later incompatible format.
Only the current version of each type is supported; earlier versions are
not migrated automatically. A tag change changes content IDs, including
those of containing records, and embedded values with earlier tags no
longer decode as their current types. ``DynamicData.try_as_type`` returns
``None`` for a different version's tag; matching tags with invalid fields
raise ``ConversionError``.
"""


def behaviors_type(name: str) -> str:
    """Qualify a versioned name such as ``Transcript.v1`` in the behaviors namespace."""
    return f"org.transluce.behaviors.{name}"
