"""Text rendering of job status and arrays for the CLI, launchers, and walkthroughs.

Meant for eyeballing output, not for round-tripping: types and ids are shown with the
display helpers (typed refs by their type id, ids shortened), struct values field by field. The
cache conflicts of a job show in groups. Each group shows as one pattern of its addresses (see
:func:`~fxtr.runner.cache_addresses.summarize`).
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any, cast

from frozendict import frozendict

from fxtr.core.entity_data import EntityID
from fxtr.entity_defns.array import Array, ArrayType
from fxtr.entity_defns.schema import Schema
from fxtr.project.client import AdoptReport, AdoptSkipReason, CacheConflict, JobStatus
from fxtr.runner.cache_addresses import AddressPattern, summarize


def format_schema(schema: Schema) -> str:
    """Render a schema for display: ``str(schema)`` (typed ids show their type id)."""
    return str(schema)


def format_type(array_type: ArrayType) -> str:
    """Render an array type for display (see :func:`format_schema`)."""
    return f"[{', '.join(map(str, array_type.dims))}]: {format_schema(array_type.schema)}"


def format_id(entity_id: EntityID) -> str:
    """Render an id for display: the first characters of its content id string."""
    return f"<{entity_id.cid_str()[:12]}…>"


def print_job_status(status: JobStatus, cache_addresses: Collection[str] | None = None) -> None:
    """Print the state of a job, its result id, its stop reason, its cache conflicts, and its
    workflow and step errors.

    Args:
        status: The status of the job.
        cache_addresses: The addresses of the cache entries. Give at least the addresses under
            the literal prefix of each conflict group. Each group then shows how many entries its
            pattern matches. If ``None``, the groups do not show this count.
    """
    print(f"job {status.job_id}: {status.state}")
    if status.result is not None:
        print(f"  result: {status.result.entity_id.cid_str()}")
    if status.reason is not None:
        print(f"  reason: {status.reason}")
    for line in format_cache_conflicts(status.cache_conflicts, cache_addresses):
        print(f"  {line}")
    for line in _conflict_commands(status):
        print(f"  {line}")
    for pathname, error in status.errors.items():
        print(f"  {pathname} failed: {error.type_name}: {error.message}")
    for cache_address, error in status.step_errors.items():
        print(f"  step {cache_address} failed: {error.type_name}: {error.message}")


def conflict_patterns(conflicts: Collection[CacheConflict]) -> list[AddressPattern]:
    """Get the patterns of the groups that :func:`format_cache_conflicts` shows.

    Args:
        conflicts: The cache conflicts of a job.

    Returns:
        One pattern for each group.
    """
    return [pattern for pattern, _ in _conflict_groups(conflicts)]


def format_cache_conflicts(
    conflicts: Collection[CacheConflict], cache_addresses: Collection[str] | None = None
) -> list[str]:
    """Make the lines that show the cache conflicts of a job.

    The first line is a heading. Then each group of conflicts has one line. All conflicts in a
    group have one kind, one owner step, and one shape. The line of a group shows:

    * The pattern of its addresses.
    * The number of its conflicts. If ``cache_addresses`` is given, the line also shows how many
      entries the pattern matches.
    * The step that owns its addresses. The line marks the step as running if its attempt runs.

    Args:
        conflicts: The cache conflicts of the job
            (:attr:`~fxtr.project.client.JobStatus.cache_conflicts`).
        cache_addresses: The addresses of the cache entries, as for :func:`print_job_status`.

    Returns:
        The lines, without indentation of the heading. No lines if there are no conflicts.
    """
    if not conflicts:
        return []
    rows: list[tuple[str, str, str]] = []
    for pattern, members in _conflict_groups(conflicts):
        if len(members) == 1:
            count = "1 address"
        elif cache_addresses is None:
            count = f"{len(members)} addresses"
        else:
            matched = sum(1 for key in cache_addresses if pattern.matches(key))
            count = f"{len(members)} of {matched} entries"
        owner = members[0].step_name
        if members[0].kind == "running":
            owner += " (running)"
        rows.append((str(pattern), count, owner))
    widths = [max(len(row[column]) for row in rows) for column in (0, 1)]
    noun = "address" if len(conflicts) == 1 else "addresses"
    lines = [f"cache conflicts at {len(conflicts)} {noun}:"]
    lines += [
        f"  {text.ljust(widths[0])}  {count.ljust(widths[1])}  {owner}"
        for text, count, owner in rows
    ]
    return lines


_SKIP_REASONS: dict[AdoptSkipReason, str] = {
    "conflict": "a different request's result is there (--overwrite-cache-conflicts replaces it)",
    "running": "an attempt runs there now",
    "equal_request": "a different result of the same request is there",
    "already": "the same result is there already",
}


def format_adopt_report(report: AdoptReport) -> list[str]:
    """Make the lines that show what a cache adoption did.

    The first line gives the number of adopted addresses. Then each reason for a skip has one line
    with its count. For conflicts and running attempts, the addresses follow in groups, one pattern
    for each group (see :func:`~fxtr.runner.cache_addresses.summarize`).

    Args:
        report: What :meth:`~fxtr.project.client.ProjectClient.adopt_cache` returned.

    Returns:
        The lines, without indentation of the first line.
    """
    count = len(report.adopted)
    lines = [f"adopted {count} cache {'entry' if count == 1 else 'entries'}"]
    for reason, text in _SKIP_REASONS.items():
        keys = [key for key, why in report.skipped.items() if why == reason]
        if not keys:
            continue
        lines.append(f"skipped {len(keys)}: {text}")
        if reason in ("conflict", "running"):
            for pattern, members in summarize(keys):
                noun = "address" if len(members) == 1 else "addresses"
                lines.append(f"  {pattern}  ({len(members)} {noun})")
    return lines


def _conflict_commands(status: JobStatus) -> list[str]:
    """Make the lines that give the commands for the cache conflicts of a job.

    Args:
        status: The status of the job.

    Returns:
        A command that lists the conflicts. If a conflict is completed, also two more commands:
        one clears the completed conflicts and resumes the job, and one resumes the job and
        overwrites the conflicts. No lines if there are no conflicts.
    """
    if not status.cache_conflicts:
        return []
    job = status.job_id
    lines = [f"to list them:          fxtr cache list --conflicts-in {job}"]
    if any(conflict.kind == "completed" for conflict in status.cache_conflicts):
        lines.append(
            f"to clear them:         fxtr cache clear --conflicts-in {job}, then fxtr resume {job}"
        )
        lines.append(f"to rerun overwriting:  fxtr resume {job} --overwrite-cache-conflicts")
    return lines


def _conflict_groups(
    conflicts: Collection[CacheConflict],
) -> list[tuple[AddressPattern, list[CacheConflict]]]:
    """Put the conflicts into groups: first by kind and step, then by shape (see
    :func:`summarize`).

    Returns:
        Each group as its pattern and its conflicts, in the order of their first addresses.
    """
    by_owner: dict[tuple[str, str], list[CacheConflict]] = {}
    for conflict in sorted(conflicts, key=lambda c: c.cache_address):
        by_owner.setdefault((conflict.kind, conflict.step_name), []).append(conflict)
    groups: list[tuple[AddressPattern, list[CacheConflict]]] = []
    for members in by_owner.values():
        by_key = {conflict.cache_address: conflict for conflict in members}
        for pattern, keys in summarize(by_key):
            groups.append((pattern, [by_key[key] for key in keys]))
    return sorted(groups, key=lambda group: group[1][0].cache_address)


def format_array(array: Array[Any]) -> str:
    """Render an array as text: one row per key, the dims as leading columns and the value last.

    An array with no dims and a struct value, such as a report, has a single row as wide as all
    its fields together, so it is rendered one field per line instead (see :func:`_format_struct`).
    """
    if not array.dim_names:
        (value,) = array.raw_values()
        if isinstance(value, frozendict):
            return "\n".join([format_type(array.type), _format_struct(value)])
    header = [*array.dim_names, format_schema(array.schema)]
    rows = [[*map(_cell, key), _cell(value)] for key, value in array.items()]
    widths = [max(len(r[i]) for r in [header, *rows]) for i in range(len(header))]
    line = "  ".join(h.ljust(w) for h, w in zip(header, widths, strict=True))
    out = [format_type(array.type), line, "  ".join("-" * w for w in widths)]
    out += ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)) for r in rows]
    return "\n".join(out)


def _format_struct(value: frozendict[str, Any]) -> str:
    """One ``name: value`` line per field, values aligned; a non-empty list field puts each
    element on its own line under the first."""
    width = max((len(name) for name in value), default=0) + 2
    lines: list[str] = []
    for name, field in value.items():
        label = f"{name}:".ljust(width)
        if isinstance(field, tuple) and field:
            items = cast("tuple[object, ...]", field)
            lines.append(f"{label}- {_cell(items[0])}")
            lines += [f"{'':{width}}- {_cell(item)}" for item in items[1:]]
        else:
            lines.append(f"{label}{_cell(field)}")
    return "\n".join(lines)


def _cell(value: object) -> str:
    match value:
        case EntityID():
            return format_id(value)
        case frozendict() | dict():
            return "{" + ", ".join(f"{k}: {_cell(v)}" for k, v in value.items()) + "}"
        case tuple() | list():
            return "[" + ", ".join(_cell(v) for v in value) + "]"
        case float():
            return f"{value:.3g}"
        case str():
            return repr(value)
        case _:
            return str(value)
