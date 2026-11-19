"""Settled execution history: job attempts, body and step attempts, events, errors, and logs.

Job record files carry this record with their facts so imports retain the evidence of how a job
ran. Only settled attempts travel. Process owners are written as ``EXPORTED_OWNER``: importing
history does not transfer an active lease or ownership of a process. Errors and logs retain
their text, including anything the job printed.

Rows use UUID strings, UTC timestamps, raw identity bytes, and JSON values. A job record of
format v0.1 carries no history.
"""

from __future__ import annotations

import dataclasses

from fxtr.core.json import FrozenJSONValue
from fxtr.entity_defns.dataclass_entity import DataclassEntity

EXECUTION_RECORD_TYPE = "org.transluce.fxtr.project.ExecutionRecord.v0.1"
"""The execution record's type tag, which carries the format's version."""

EXPORTED_OWNER = "exported"
"""What an execution record writes as every job attempt's lease owner."""


@dataclasses.dataclass(frozen=True, kw_only=True)
class JobAttemptRow:
    """A ``job_attempts`` row, settled."""

    id: str
    job_id: str
    state: str
    reason: str | None
    owner: str
    heartbeat_at: str
    settled_at: str | None
    evicted_at: str | None
    evicted_reason: str | None
    overwrite_cache_conflicts: bool


@dataclasses.dataclass(frozen=True, kw_only=True)
class AttemptRow:
    """An ``attempts`` row: a workflow body's attempt or a step's, closed."""

    id: str
    kind: str
    invocation_id: str | None
    cache_key: str | None
    request: bytes | None
    owner_job_attempt_id: str
    state: str
    error: FrozenJSONValue
    reason: str | None
    stopped_on: FrozenJSONValue
    started_at: str
    finished_at: str | None


@dataclasses.dataclass(frozen=True, kw_only=True)
class AttemptEventRow:
    """An ``attempt_events`` row."""

    attempt_id: str
    seq: int
    kind: str
    identity: bytes
    at: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class AttemptLogRow:
    """An ``attempt_logs`` row."""

    attempt_id: str
    seq: int
    message: str
    at: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class ExecutionRecord(DataclassEntity, fxtr_type=EXECUTION_RECORD_TYPE):
    """How one job's attempts went (see the module docstring).

    Attributes:
        job_id: The job.
        job_attempts: Its job attempts.
        attempts: The attempts they owned: of the job's invocations, and of the steps they ran.
        events: Those attempts' events.
        logs: Those attempts' log lines.
    """

    job_id: str
    job_attempts: tuple[JobAttemptRow, ...]
    attempts: tuple[AttemptRow, ...]
    events: tuple[AttemptEventRow, ...]
    logs: tuple[AttemptLogRow, ...]


def validate_execution_record(record: ExecutionRecord) -> None:
    """Check an execution record's rows against each other and against what the runner's tables
    accept.

    Raises:
        RecordFileError: If a job attempt is of another job or not settled, or names a process;
            an attempt's owner is not among the job attempts, or it is still running; a state, a
            kind, or an attempt's kind and its columns are not a combination the runner's tables
            accept; an event or log line names an attempt the record lacks; or two rows share
            their key.
    """
    from fxtr.project.job_record import RecordFileError

    # The values the runner's tables accept, as the check constraints of fxtr.runner.postgres
    # .schema state them (job_attempts_state, attempts_kind, attempts_state, attempts_kind_columns,
    # attempt_events_kind). Refusing them here keeps an import a refusal rather than a database
    # error; tests/test_runner_postgres_job_records.py checks the two agree.
    settled_job_attempt_states = {"succeeded", "stopped", "cancelled"}
    attempt_kinds = {"workflow", "step"}
    closed_attempt_states = {"completed", "failed", "interrupted", "abandoned"}
    event_kinds = {"emit", "observe", "return"}

    owners = {row.id for row in record.job_attempts}
    if len(owners) != len(record.job_attempts):
        raise RecordFileError("the execution record has two rows for one job attempt")
    for row in record.job_attempts:
        if row.job_id != record.job_id:
            raise RecordFileError(f"job attempt {row.id} is of another job")
        if row.state == "active" or row.owner != EXPORTED_OWNER:
            raise RecordFileError(f"job attempt {row.id} is not a settled, exported one")
        if row.state not in settled_job_attempt_states:
            raise RecordFileError(f"job attempt {row.id} has an unknown state {row.state!r}")
    attempts = {row.id for row in record.attempts}
    if len(attempts) != len(record.attempts):
        raise RecordFileError("the execution record has two rows for one attempt")
    for row in record.attempts:
        if row.owner_job_attempt_id not in owners:
            raise RecordFileError(f"attempt {row.id}'s job attempt is not in the record")
        if row.state == "running":
            raise RecordFileError(f"attempt {row.id} is still running")
        if row.state not in closed_attempt_states:
            raise RecordFileError(f"attempt {row.id} has an unknown state {row.state!r}")
        if row.kind not in attempt_kinds:
            raise RecordFileError(f"attempt {row.id} has an unknown kind {row.kind!r}")
        # A workflow attempt is of an invocation; a step attempt is of a request at a cache key.
        if row.kind == "workflow":
            names = "its invocation alone"
            well_formed = (
                row.invocation_id is not None and row.cache_key is None and row.request is None
            )
        else:
            names = "a cache key and a request"
            well_formed = (
                row.invocation_id is None and row.cache_key is not None and row.request is not None
            )
        if not well_formed:
            raise RecordFileError(
                f"attempt {row.id} is a {row.kind} attempt but does not name {names}"
            )
    for rows, what in ((record.events, "event"), (record.logs, "log line")):
        keys = {(row.attempt_id, row.seq) for row in rows}
        if len(keys) != len(rows):
            raise RecordFileError(f"the execution record has two rows for one {what}")
        for attempt_id, _ in keys:
            if attempt_id not in attempts:
                raise RecordFileError(f"a {what} names attempt {attempt_id}, which it lacks")
    for event in record.events:
        if event.kind not in event_kinds:
            raise RecordFileError(
                f"event {event.seq} of attempt {event.attempt_id} has an unknown kind"
                f" {event.kind!r}"
            )


__all__ = [
    "EXECUTION_RECORD_TYPE",
    "EXPORTED_OWNER",
    "AttemptEventRow",
    "AttemptLogRow",
    "AttemptRow",
    "ExecutionRecord",
    "JobAttemptRow",
    "validate_execution_record",
]
