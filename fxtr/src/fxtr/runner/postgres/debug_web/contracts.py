"""Typed GraphQL records for the viewer's summary and paging API.

GraphQL validates their fixed shapes. JSON remains confined to array types, node parameters,
keys and cell contents. Records use GraphQL field names; the DateTime scalar serializes native
datetimes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, NotRequired, TypedDict

type JSONValue = None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]
type JSONObject = dict[str, JSONValue]

type DisplayState = Literal[
    "queued", "running", "completed", "failed", "interrupted", "conflict", "stopped"
]
STATE_NAMES: tuple[DisplayState, ...] = (
    "queued",
    "running",
    "completed",
    "failed",
    "interrupted",
    "conflict",
    "stopped",
)
type StateCounts = dict[DisplayState, int]
type Key = str | int


def state_counts() -> StateCounts:
    return dict.fromkeys(STATE_NAMES, 0)


class ErrorView(TypedDict):
    typeName: str
    message: str
    traceback: str


class NodeInputView(TypedDict):
    label: JSONValue
    identity: str


class NodeFields(TypedDict):
    identity: str
    kind: str
    name: str | None
    type: JSONObject
    inputs: list[NodeInputView]
    valueCid: str | None


class NodeView(NodeFields):
    id: str
    params: JSONObject


class RunActivity(TypedDict):
    """When a run's invocations ran, counting only attempts this job ran (a result bound from
    another job's cache took no time here)."""

    firstStartedAt: datetime | None
    lastFinishedAt: datetime | None
    finished: int
    """How many invocations this job finished, completed or failed."""
    oldestRunningSince: datetime | None
    """When the longest-running invocation still running started, whoever runs it."""


class FailureGroup(TypedDict):
    typeName: str
    message: str
    """One of the group's messages, as an example."""
    count: int


class OverviewNode(NodeFields):
    params: JSONObject
    mapOver: list[str]
    replicaDims: list[str]
    counts: StateCounts | None
    activity: RunActivity | None
    failures: list[FailureGroup] | None
    version: str


class LevelOverview(TypedDict):
    pathname: str
    workflowName: str
    state: DisplayState
    resultCid: str | None
    error: ErrorView | None
    startedAt: datetime | None
    finishedAt: datetime | None
    nodes: list[OverviewNode]
    attemptCount: int
    codeVersion: str | None
    """The CID of the code version the latest attempt ran (its owner job attempt's); ``None``
    before an attempt."""
    returnedNode: str | None
    awaitingNode: str | None
    version: str
    asOf: datetime
    """The database's time when the overview was read, the clock its timestamps are on."""


class SpawnView(TypedDict):
    nodeIdentity: str
    key: list[Key]
    childPathname: str | None
    stepCacheKey: str | None
    resultCid: str | None
    state: DisplayState


class JobSummary(TypedDict):
    id: str
    workflowName: str
    root: str
    createdAt: datetime
    state: Literal["running", "succeeded", "stopped"]
    failed: bool


class JobPage(TypedDict):
    items: list[JobSummary]
    next: str | None


class RunPage(TypedDict):
    items: list[SpawnView]
    next: str | None
    total: int
    offset: int | None


class KeyItem(TypedDict):
    value: Key
    compatible: bool
    counts: StateCounts


class KeyPage(TypedDict):
    items: list[KeyItem]
    next: str | None
    total: int
    offset: int | None


class DimValue(TypedDict):
    value: Key
    compatible: bool


class DimValuePage(TypedDict):
    items: list[DimValue]
    total: int
    offset: int


class EntityMeta(TypedDict):
    cid: str
    typeTag: str


class OutputSource(TypedDict):
    cid: NotRequired[str | None]
    jobId: NotRequired[str | None]
    pathname: NotRequired[str | None]
    nodeId: NotRequired[str | None]
    selection: NotRequired[dict[str, Key] | None]


class Sort(TypedDict):
    key: str
    direction: Literal["asc", "desc"]


class ColumnFilter(TypedDict):
    key: str
    text: str


class OutputRow(TypedDict):
    key: list[Key]
    value: str


class InvocationRow(TypedDict):
    spawn: SpawnView
    rowCount: int | None
    """How many rows its result has; null without a result."""
    value: str | None
    """Its one row's value as base64 DRISL, when every invocation produces one row."""


class InvocationPage(TypedDict):
    items: list[InvocationRow]
    total: int
    offset: int


class OutputPage(TypedDict):
    type: JSONObject | None
    rows: list[OutputRow]
    total: int | None
    offset: int
    hasMore: bool
