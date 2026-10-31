"""Typed node and error projections shared by detailed resolvers and overview readers."""

from fxtr.runner.nodes import NodeRow, RunChildWorkflow, RunStep, node_name, to_row
from fxtr.runner.postgres.json_values import array_type_to_json
from fxtr.runner.postgres.ledger import JobID, NodeRecord
from fxtr.runner.session import AttemptError

from .contracts import (
    ErrorView,
    FailureGroup,
    NodeFields,
    NodeView,
    OverviewNode,
    RunActivity,
    StateCounts,
    state_counts,
)


def error_view(error: AttemptError | None) -> ErrorView | None:
    if error is None:
        return None
    return {"typeName": error.type_name, "message": error.message, "traceback": error.traceback}


def _node_fields(record: NodeRecord, row: NodeRow) -> NodeFields:
    return {
        "identity": record.identity.cid_str(),
        "kind": row.kind,
        "name": node_name(record.node),
        "type": array_type_to_json(record.type),
        "inputs": [
            {"label": label, "identity": identity.cid_str()} for label, identity in row.inputs
        ],
        "valueCid": None if record.value is None else record.value.entity_id.cid_str(),
    }


def node_view(record: NodeRecord, job_id: JobID, pathname: str) -> NodeView:
    row = to_row(record.node)
    return {
        **_node_fields(record, row),
        "id": f"{job_id}/{pathname}/{record.identity.cid_str()}",
        "params": row.params,
    }


def no_activity() -> RunActivity:
    return {
        "firstStartedAt": None,
        "lastFinishedAt": None,
        "finished": 0,
        "oldestRunningSince": None,
    }


def overview_node(
    record: NodeRecord,
    counts: StateCounts | None,
    activity: RunActivity | None,
    failures: list[FailureGroup] | None,
    version: str,
) -> OverviewNode:
    row = to_row(record.node)
    run = record.node if isinstance(record.node, RunStep | RunChildWorkflow) else None
    return {
        **_node_fields(record, row),
        "params": {
            key: value for key, value in row.params.items() if key not in ("map_over", "replicas")
        },
        "mapOver": [] if run is None else list(run.map_over),
        "replicaDims": [] if run is None else list(run.replicas),
        "counts": (counts if counts is not None else state_counts()) if run is not None else None,
        "activity": (activity if activity is not None else no_activity())
        if run is not None
        else None,
        "failures": (failures if failures is not None else []) if run is not None else None,
        "version": version,
    }
