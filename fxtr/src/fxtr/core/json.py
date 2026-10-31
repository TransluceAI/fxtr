"""JSON-only annotations shared by entity fields and external JSON codecs.

The recursive unions keep dictionaries before lists for Mashumaro compatibility. Numeric
range and finiteness are checked by the storage codec, independently of these type aliases.

Each alias has a frozen counterpart (frozendicts and tuples in place of dicts and lists) for
fields of hashable, immutable entities. Both shapes store the same wire data.
"""

from frozendict import frozendict

type JSONValue = str | int | float | bool | None | dict[str, JSONValue] | list[JSONValue]
"""A JSON scalar, object, or array; bytes and entity references are not JSON values."""

type JSONObject = dict[str, JSONValue]
"""A JSON object with string keys and recursively JSON-only values."""

type FrozenJSONValue = (
    str | int | float | bool | None | frozendict[str, FrozenJSONValue] | tuple[FrozenJSONValue, ...]
)
"""Immutable, hashable counterpart of :data:`JSONValue` (frozendicts and tuples)."""

type FrozenJSONObject = frozendict[str, FrozenJSONValue]
"""Immutable, hashable counterpart of :data:`JSONObject`."""
