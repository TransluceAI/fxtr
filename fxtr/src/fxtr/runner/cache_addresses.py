"""Cache addresses split into their parts, and patterns that match many keys of one shape.

A cache address is the cache address prefix of a run node, followed by its mapped key and its
replica key (see :func:`~fxtr.runner.steps.cache_key` and
:func:`~fxtr.experiment.workflows.append_mapped_key`). A key thus has two kinds of parts:

* Literal text, for example ``root/screen``.
* Mapped key groups, for example ``[applicant:"ada",n:3]``. Each value in a group is JSON.

:func:`parse_address` splits a key into these parts. A group starts with ``[``, has one or more
``dim:value`` slots separated by ``,``, and ends with ``]``. If the text after a ``[`` is not a
correct group, that ``[`` is literal text. Thus every string parses.

A :class:`AddressPattern` is a key in which a value can be ``*``. The ``*`` matches any one value. A
bare ``*`` is not a JSON value, so a pattern does not need escapes. For example,
``root/screen[applicant:*]/pangram`` matches that key for each value of ``applicant``.

:func:`summarize` puts keys into groups by *shape*. The shape of a key is the key without its
values. For each group, :func:`summarize` makes one pattern. The pattern keeps a value if all
keys in the group have that value. It puts ``*`` where the values are different.

:func:`under_prefix` is the prefix test that the cache selections use.
:func:`~fxtr.runner.postgres.execution.select_cache_entries` makes the same test in SQL. A
selection can also be a pattern: :meth:`AddressPattern.selects` tells if a key is at or under a key
that the pattern matches. A ``*`` stands only for a value. A ``*`` anywhere else in a pattern is
an error (:class:`PatternError`), because it does not mean what a glob means.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterable, Sequence

_DECODER = json.JSONDecoder()


class PatternError(ValueError):
    """A pattern has a ``*`` in a place other than a value."""


@dataclasses.dataclass(frozen=True)
class Slot:
    """One ``dim:value`` slot of a mapped key group.

    Attributes:
        dim: The name of the dim.
        value: The value as canonical JSON text, as ``json.dumps`` writes it. In a pattern,
            ``None`` is the wildcard ``*``.
    """

    dim: str
    value: str | None


type Group = tuple[Slot, ...]
"""A mapped key group: its slots, in their order in the key."""

type Part = str | Group
"""One part of a key: literal text or a mapped key group."""


@dataclasses.dataclass(frozen=True)
class AddressPattern:
    """A cache address, or a pattern that matches cache addresses. A wildcard slot matches any
    value.

    Attributes:
        parts: The literal text and the mapped key groups, in order. Two literal parts are never
            adjacent: adjacent literal text is one part.
    """

    parts: tuple[Part, ...]

    @property
    def shape(self) -> tuple[str | tuple[str, ...], ...]:
        """The parts without their values: the literal text, and the dims of each group."""
        return tuple(
            part if isinstance(part, str) else tuple(slot.dim for slot in part)
            for part in self.parts
        )

    @property
    def has_wildcards(self) -> bool:
        """``True`` if a slot of the pattern is a wildcard."""
        return any(
            slot.value is None for part in self.parts if not isinstance(part, str) for slot in part
        )

    @property
    def literal_prefix(self) -> str:
        """The literal text before the first group. Each key that the pattern matches starts
        with this text. If the pattern has a group, a ``[`` follows the text in each such key."""
        first = self.parts[0] if self.parts else ""
        return first if isinstance(first, str) else ""

    def matches(self, cache_key: str) -> bool:
        """Tell if the pattern matches a key.

        Args:
            cache_key: The key to examine.

        Returns:
            ``True`` if the key has the shape of the pattern and has the value of the pattern in
            each slot that is not a wildcard. ``False`` otherwise.
        """
        key = parse_address(cache_key)
        if key.shape != self.shape:
            return False
        return all(
            mine.value is None or mine.value == theirs.value
            for own, other in zip(self.parts, key.parts, strict=True)
            if not isinstance(own, str) and not isinstance(other, str)
            for mine, theirs in zip(own, other, strict=True)
        )

    def selects(self, cache_key: str) -> bool:
        """Tell if a key is at or under a key that the pattern matches.

        Without wildcards, this is :func:`under_prefix` with the text of the pattern. With
        wildcards, all parts of the pattern but the last must match the first parts of the key.
        If the last part is a group, it must match the next part of the key. If the last part is
        literal text, the next part of the key must start with that text, followed by ``/``,
        ``[``, or nothing.

        Args:
            cache_key: The key to examine.

        Returns:
            ``True`` if the pattern selects the key.
        """
        if not self.has_wildcards:
            return under_prefix(cache_key, str(self))
        key = parse_address(cache_key).parts
        if len(key) < len(self.parts):
            return False
        *exact, last = range(len(self.parts))
        if not all(_part_matches(self.parts[i], key[i]) for i in exact):
            return False
        mine, theirs = self.parts[last], key[last]
        if not isinstance(mine, str):
            return _part_matches(mine, theirs)
        if not isinstance(theirs, str) or not theirs.startswith(mine):
            return False
        if len(theirs) > len(mine):
            return theirs[len(mine)] in "/["
        return True

    def __str__(self) -> str:
        return "".join(_render(part) for part in self.parts)


def parse_address(text: str) -> AddressPattern:
    """Split a cache address into its parts.

    Args:
        text: The key. A ``*`` in a slot is not a wildcard here, so that slot is not a correct
            group and its text stays literal.

    Returns:
        The parts of the key. Each value is canonical JSON text.
    """
    return _parse(text, wildcards=False)


def parse_pattern(text: str) -> AddressPattern:
    """Split a pattern into its parts.

    Args:
        text: The pattern: a cache address in which a value can be ``*``.

    Returns:
        The parts of the pattern. A ``*`` value becomes a wildcard slot.

    Raises:
        PatternError: If a ``*`` is in a place other than a value.
    """
    pattern = _parse(text, wildcards=True)
    for part in pattern.parts:
        names = [part] if isinstance(part, str) else [slot.dim for slot in part]
        if any("*" in name for name in names):
            raise PatternError(
                f"{text!r}: a * stands only for a whole mapped value, as in root/step[dim:*]"
            )
    return pattern


def summarize(cache_addresses: Iterable[str]) -> list[tuple[AddressPattern, list[str]]]:
    """Put keys into groups by shape, and make one pattern for each group.

    In a slot where all keys of the group have the same value, the pattern keeps that value. In
    the other slots, the pattern has ``*``. Thus the pattern for a group of one key is that key.

    Args:
        cache_addresses: The addresses.

    Returns:
        Each group as its pattern and its keys. The groups are in the order of their first keys.
        In each group, the addresses are in their order in ``cache_addresses``.
    """
    groups: dict[tuple[str | tuple[str, ...], ...], list[tuple[str, AddressPattern]]] = {}
    for cache_address in cache_addresses:
        parsed = parse_address(cache_address)
        groups.setdefault(parsed.shape, []).append((cache_address, parsed))
    summary: list[tuple[AddressPattern, list[str]]] = []
    for members in groups.values():
        first = members[0][1]
        parts: list[Part] = []
        for index, part in enumerate(first.parts):
            if isinstance(part, str):
                parts.append(part)
                continue
            parts.append(
                tuple(
                    Slot(slot.dim, slot.value if _agree(members, index, n) else None)
                    for n, slot in enumerate(part)
                )
            )
        summary.append((AddressPattern(tuple(parts)), [key for key, _ in members]))
    return summary


def under_prefix(cache_address: str, prefix: str) -> bool:
    """Tell if a cache address is a prefix, or is under that prefix.

    A key is under a prefix if the key starts with the prefix and the next character is ``/`` or
    ``[``. For example, ``root/refine`` selects ``root/refine[bio:"ada"]/rewrite``, because the
    mapped key of a path component follows its name. ``root/refine`` does not select
    ``root/refined``.

    Args:
        cache_address: The address.
        prefix: The prefix. The empty prefix ``""`` selects all addresses.

    Returns:
        ``True`` if the key is the prefix or is under it.
    """
    if prefix == "" or cache_address == prefix:
        return True
    return cache_address.startswith(prefix) and cache_address[len(prefix)] in "/["


def _part_matches(mine: Part, theirs: Part) -> bool:
    """Tell if a part of a pattern matches a part of a key. Literal text must be equal. A group
    must have the same dims, and the same value in each slot that is not a wildcard."""
    if isinstance(mine, str) or isinstance(theirs, str):
        return mine == theirs
    if tuple(slot.dim for slot in mine) != tuple(slot.dim for slot in theirs):
        return False
    return all(a.value is None or a.value == b.value for a, b in zip(mine, theirs, strict=True))


def _agree(members: Sequence[tuple[str, AddressPattern]], index: int, n: int) -> bool:
    """Tell if all members have the same value in one slot.

    Args:
        members: The keys of one group, as text and parsed. All have one shape.
        index: The position of the group in the parts.
        n: The position of the slot in that group.
    """
    values = set[str | None]()
    for _, parsed in members:
        group = parsed.parts[index]
        assert not isinstance(group, str)
        values.add(group[n].value)
    return len(values) == 1


def _render(part: Part) -> str:
    if isinstance(part, str):
        return part
    slots = ",".join(f"{slot.dim}:{'*' if slot.value is None else slot.value}" for slot in part)
    return f"[{slots}]"


def _parse(text: str, *, wildcards: bool) -> AddressPattern:
    parts: list[Part] = []
    literal: list[str] = []
    position = 0
    while position < len(text):
        bracket = text.find("[", position)
        if bracket < 0:
            literal.append(text[position:])
            break
        literal.append(text[position:bracket])
        parsed = _parse_group(text, bracket, wildcards=wildcards)
        if parsed is None:
            literal.append("[")
            position = bracket + 1
            continue
        group, position = parsed
        if literal:
            parts.append("".join(literal))
            literal = []
        parts.append(group)
    if "".join(literal):
        parts.append("".join(literal))
    return AddressPattern(tuple(_merge(parts)))


def _parse_group(text: str, start: int, *, wildcards: bool) -> tuple[Group, int] | None:
    """Parse the group that starts at a ``[``.

    Args:
        text: The key or pattern.
        start: The position of the ``[``.
        wildcards: If ``True``, a ``*`` value is a wildcard. If ``False``, a ``*`` value makes
            the group incorrect.

    Returns:
        The group and the position after its ``]``. ``None`` if the text is not a correct group.
    """
    slots: list[Slot] = []
    position = start + 1
    while True:
        colon = text.find(":", position)
        if colon <= position:
            return None
        dim = text[position:colon]
        if any(c in dim for c in "[],"):
            return None
        position = colon + 1
        if wildcards and text.startswith("*", position):
            value: str | None = None
            position += 1
        else:
            try:
                decoded, end = _DECODER.raw_decode(text, position)
            except json.JSONDecodeError:
                return None
            value = json.dumps(decoded, ensure_ascii=False)
            position = end
        slots.append(Slot(dim, value))
        if text.startswith(",", position):
            position += 1
        elif text.startswith("]", position):
            return tuple(slots), position + 1
        else:
            return None


def _merge(parts: Iterable[Part]) -> list[Part]:
    """Join adjacent literal text into one part, and remove empty text."""
    merged: list[Part] = []
    for part in parts:
        if isinstance(part, str):
            if not part:
                continue
            if merged and isinstance(merged[-1], str):
                merged[-1] = merged[-1] + part
                continue
        merged.append(part)
    return merged


__all__ = [
    "Group",
    "AddressPattern",
    "Part",
    "PatternError",
    "Slot",
    "parse_address",
    "parse_pattern",
    "summarize",
    "under_prefix",
]
