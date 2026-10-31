"""Table text shared with the browser's ``frontend/src/lib/values.ts`` contract.

Integers stay exact decimal strings. Floats use shortest round-trip digits with ECMAScript's
fixed/scientific notation boundary (-6 through 20 use fixed notation), unpadded signed
exponents, and zero without a sign. Containers search their count summary and full preview;
strings in previews use JSON quoting, entity links show their short cid, and maps use
JavaScript property enumeration order.
``frontend/test/fixtures/table-values.json`` verifies both runtimes against identical CBOR.
"""

import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from fxtr.core.entity_data import EntityID

# The browser's short cid (``shortCid`` in the fxtr-view package): the prefix every cid shares
# dropped, then the next eight characters.
COMMON_CID_PREFIX = "bafyrei"
SHORT_CID_LENGTH = 8


def number_text(value: float) -> str:
    if value == 0:
        return "0"
    text = repr(value)
    if 1e-6 <= abs(value) < 1e21:
        fixed = format(Decimal(text), "f")
        return fixed.rstrip("0").rstrip(".") if "." in fixed else fixed
    mantissa, exponent = text.split("e")
    return f"{mantissa.removesuffix('.0')}e{int(exponent):+d}"


def object_items(value: Mapping[str, Any]):
    """JavaScript enumerates array-index property names first, then other names in wire order."""

    def index(key: str) -> int | None:
        if key.isascii() and key.isdecimal() and len(key) <= 10:
            number = int(key)
            if str(number) == key and number < 2**32 - 1:
                return number
        return None

    indices = sorted((number, key) for key in value if (number := index(key)) is not None)
    keys = [key for _, key in indices] + [key for key in value if index(key) is None]
    return ((key, value[key]) for key in keys)


def summary(value: Any) -> str:
    """The table's display/search summary, with deterministic Unicode text ordering."""
    if value is None:
        return "—"
    if isinstance(value, EntityID):
        text = value.cid_str().removeprefix(COMMON_CID_PREFIX)
        return f"<{text[:SHORT_CID_LENGTH]}>"
    if isinstance(value, bytes):
        return f"{len(value)} bytes"
    if isinstance(value, (list, tuple)):
        return f"{len(value)} {'item' if len(value) == 1 else 'items'}"
    if isinstance(value, Mapping):
        return "{" + f"{len(value)} {'field' if len(value) == 1 else 'fields'}" + "}"
    if isinstance(value, bool):
        return "true" if value else "false"
    return number_text(value) if isinstance(value, float) else str(value)


def preview(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(preview(v) for v in value) + "]"
    if isinstance(value, Mapping):
        return (
            "{"
            + ", ".join(
                f"{json.dumps(k, ensure_ascii=False)}: {preview(v)}" for k, v in object_items(value)
            )
            + "}"
        )
    return summary(value)


def search_text(value: Any) -> str:
    text = summary(value)
    if isinstance(value, (list, tuple, Mapping)):
        text += " " + preview(value)
    return text.lower()


def compare_cells(left: Any, right: Any) -> int:
    if type(left) not in (int, float) or type(right) not in (int, float):
        left, right = summary(left), summary(right)
    return (left > right) - (left < right)
