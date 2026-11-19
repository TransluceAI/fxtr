"""The runner's tables as fxtr first managed them with migrations.

Revision ID: 0001
Revises: (none)
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "entities",
        sa.Column("cid", sa.LargeBinary(), nullable=False),
        sa.Column("type_tag", sa.Text(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("cid"),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False, comment="The job id, minted by the creator."),
        sa.Column(
            "workflow_name",
            sa.Text(),
            nullable=False,
            comment="The root workflow's name (fxtr.experiment.workflows.Workflow.name).",
        ),
        sa.Column(
            "root",
            sa.Text(),
            nullable=False,
            comment="The root invocation's pathname and default cache key prefix (the job's 'root' argument; 'root' by default). Pathnames are scoped to the job, cache keys are shared across jobs, so jobs of different root workflows sharing one step cache pick distinct roots.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        comment="One request to execute a root workflow with given inputs. Its state is derived from its invocations and attempts, never stored (see the module docstring); how it is being executed is the job_attempts' business.",
    )
    op.create_table(
        "node_inputs",
        sa.Column("invocation_id", sa.Uuid(), nullable=False),
        sa.Column("node_identity", sa.LargeBinary(), nullable=False),
        sa.Column(
            "label",
            postgresql.JSONB(astext_type=Text()),
            nullable=False,
            comment="How the node knows this input: an argument name (combine, run nodes), a key value along the new dim (stack; an int or a str), or the position the query plan's QuerySource reads it at (query; an int). One query node may read the same input at several positions, so these rows are not one per input.",
        ),
        sa.Column(
            "input_identity",
            sa.LargeBinary(),
            nullable=False,
            comment="The identity of the input node (a node of the same invocation).",
        ),
        sa.PrimaryKeyConstraint("invocation_id", "node_identity", "label"),
        comment="The edges of every invocation's DAG: one row per input of a node, with the label the node knows it by. Together with nodes.params this is the whole node.",
    )
    op.create_table(
        "project_arrays",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("type", postgresql.JSONB(astext_type=Text()), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("name"),
    )
    op.create_table(
        "job_attempt_provenance",
        sa.Column(
            "id",
            sa.Uuid(),
            nullable=False,
            comment="The job attempt's id: the same as its job_attempts row, where this database ran it.",
        ),
        sa.Column(
            "job_id",
            sa.Uuid(),
            nullable=False,
            comment="The job the job attempt ran. Deliberately not a foreign key: this database may hold the fact without the job, when it arrived with a result another job took from the cache (see the module docstring).",
        ),
        sa.Column(
            "code_version_cid",
            sa.LargeBinary(),
            nullable=False,
            comment="The code the job attempt's process ran: a CodeVersion entity (fxtr.project.code_version): the commit its checkout pinned when it launched or resumed the job, the project within the repository, whether the environment was checked, and where each function it could run is implemented. Every attempt the job attempt owned ran at it. The job's launch version is its first job attempt's; a later job attempt may run at another commit and says so here.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="When the job attempt was created: the wall clock at the insert, not the start of its transaction, so a job's job attempts are in creation order by it (ties broken by id). Their positions among the job's (1, 2, ...) are counted from it when read.",
        ),
        sa.ForeignKeyConstraint(
            ["code_version_cid"],
            ["entities.cid"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "job_id", name="job_attempt_provenance_id_job"),
        comment="That a job attempt existed and which code its process ran: the immutable fact about a job attempt, written with it and never changed. Results point here for their provenance. How the job attempt went (its lease, its state) is execution state, in job_attempts, which exists only for job attempts this database ran.",
    )
    op.create_index(
        "job_attempt_provenance_job_created",
        "job_attempt_provenance",
        ["job_id", "created_at", "id"],
        unique=False,
    )
    op.create_table(
        "project_array_rows",
        sa.Column("array_name", sa.Text(), nullable=False),
        sa.Column("key", postgresql.JSONB(astext_type=Text()), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=Text()), nullable=False),
        sa.ForeignKeyConstraint(["array_name"], ["project_arrays.name"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("array_name", "key"),
    )
    op.create_table(
        "project_array_snapshots",
        sa.Column("array_name", sa.Text(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("cid", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["array_name"], ["project_arrays.name"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["cid"],
            ["entities.cid"],
        ),
        sa.PrimaryKeyConstraint("array_name", "version"),
    )
    op.create_table(
        "step_requests",
        sa.Column(
            "request_cid",
            sa.LargeBinary(),
            nullable=False,
            comment="The request's fingerprint: the cid of the StepRequest entity (fxtr.runner.steps) in the entities table, which holds the whole call including the input arrays.",
        ),
        sa.Column(
            "step_name",
            sa.Text(),
            nullable=False,
            comment="The step the request calls, as the entity records it.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["request_cid"],
            ["entities.cid"],
        ),
        sa.PrimaryKeyConstraint("request_cid"),
        comment="The relational face of every step request the system has seen: the fields of the request entity a query needs (claim matches a worker's StepSpec on step_name), keyed by the fingerprint, the way nodes are stored as rows beside their identity. One row per distinct call, written when the call is first requested and never changed; attempts and cache entries reference it.",
    )
    op.create_table(
        "job_attempts",
        sa.Column(
            "id",
            sa.Uuid(),
            nullable=False,
            comment="The job attempt id, minted by the creator; its fact (which code it ran, when it was created) is the job_attempt_provenance row with the same id and job.",
        ),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            comment="'active': directing the job's work (at most one per job, enforced by job_attempts_one_active). Then exactly one of: 'succeeded' (the root's result was materialized while it was active), 'stopped' (no attempt of the job was live and the root unfinished: everything it scheduled has failed, been interrupted, or was never replaced), 'cancelled' (by a person; see reason). A settled job attempt never returns to active; resuming creates a new one.",
        ),
        sa.Column(
            "reason",
            sa.Text(),
            nullable=True,
            comment="For state 'cancelled': why, as the person gave it.",
        ),
        sa.Column(
            "owner",
            sa.Text(),
            nullable=False,
            comment="The process that runs this job attempt (hostname, pid, and a random token; display only). Together with heartbeat_at, the lease.",
        ),
        sa.Column(
            "heartbeat_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="When the owner last refreshed its lease. The job attempt is fresh while it is active, not evicted, and this is recent (the execution policy's staleness threshold); stale otherwise, when its process is presumed dead and a resume may take it over.",
        ),
        sa.Column(
            "evicted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Set when another process took the job attempt over or a hard cancel stopped it: every attempt it owned was abandoned, and its process finds out on its next heartbeat. Distinct from settling: an evicted job attempt is also settled (stopped or cancelled).",
        ),
        sa.Column("evicted_reason", sa.Text(), nullable=True, comment="For evicted_at: why."),
        sa.Column(
            "overwrite_cache_conflicts",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
            comment="Whether this job attempt overwrites its cache conflicts with completed results: while it is active, a request of its job at a key that offers a different request's step result runs, and takes the entry. A conflict with a running attempt is not overwritten. Set when the job attempt is created, by the launch or resume that asked for it; a later job attempt does not inherit it.",
        ),
        sa.Column(
            "settled_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When it left 'active'.",
        ),
        sa.CheckConstraint(
            "state IN ('active', 'succeeded', 'stopped', 'cancelled')", name="job_attempts_state"
        ),
        sa.ForeignKeyConstraint(
            ["id", "job_id"],
            ["job_attempt_provenance.id", "job_attempt_provenance.job_id"],
            name="job_attempts_provenance",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        comment="One process's episode of running a job, as this database runs it: the scheduling policy's state for it and the lease its owner holds (which code it runs, and when it was created, are its job_attempt_provenance fact). The active job attempt decides what is due to run and whether an unfinished target is pending or stopped, and owns every attempt its process runs (attempts.owner_job_attempt_id). Settling it (succeeded, stopped, cancelled) means it stops deciding; evicting it (evicted_at) abandons what it owns.",
    )
    op.create_index(
        "job_attempts_one_active",
        "job_attempts",
        ["job_id"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
    )
    op.create_table(
        "job_step_requests",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("cache_key", sa.Text(), nullable=False, comment="The cache key requested."),
        sa.Column(
            "request_cid",
            sa.LargeBinary(),
            nullable=False,
            comment="The request (its fingerprint) the job made at the key. One per key per job: a body asking for a different request at a key its job already requested used one key for two configurations, a failure of the requesting attempt.",
        ),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["request_cid"],
            ["step_requests.request_cid"],
        ),
        sa.PrimaryKeyConstraint("job_id", "cache_key"),
        comment="What each job asked for at each cache key: a fact about the workflow, written once by the first attempt that requests the key and matched by every later one (replay).",
    )
    op.create_table(
        "step_results",
        sa.Column(
            "id",
            sa.Uuid(),
            nullable=False,
            comment="The step result's id: the id of the step attempt that produced it, where this database ran that attempt (attempts.id). Deliberately not a foreign key: a database may hold a step result without the attempt, when the result came from elsewhere.",
        ),
        sa.Column(
            "request_cid",
            sa.LargeBinary(),
            nullable=False,
            comment="The step request that was run (its fingerprint).",
        ),
        sa.Column(
            "result_cid",
            sa.LargeBinary(),
            nullable=False,
            comment="What it produced (an Array entity).",
        ),
        sa.Column(
            "job_attempt_id",
            sa.Uuid(),
            nullable=False,
            comment="The job attempt whose process ran it (the producing attempt's owner): which code produced the result. Another job's, for a result jobs took from the cache.",
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="When the producing attempt completed.",
        ),
        sa.ForeignKeyConstraint(
            ["job_attempt_id"],
            ["job_attempt_provenance.id"],
        ),
        sa.ForeignKeyConstraint(
            ["request_cid"],
            ["step_requests.request_cid"],
        ),
        sa.ForeignKeyConstraint(
            ["result_cid"],
            ["entities.cid"],
        ),
        sa.PrimaryKeyConstraint("id"),
        comment="Every completed run of a step: the fact that a request, run under a job attempt, produced a result. Written once, when the step attempt completes, and never changed; not keyed by job or cache key, since jobs share results through the cache. Bindings and completed cache entries point here.",
    )
    op.create_table(
        "workflow_invocations",
        sa.Column(
            "id", sa.Uuid(), nullable=False, comment="The invocation id, minted by the creator."
        ),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column(
            "pathname",
            sa.Text(),
            nullable=False,
            comment="The invocation's path in the experiment (fxtr.experiment.workflows.join_path / append_mapped_key), unique within the job: the root's is the job's root; a child's is its parent's pathname, the run node's name, and the mapped key.",
        ),
        sa.Column(
            "workflow_name",
            sa.Text(),
            nullable=False,
            comment="The workflow to run (fxtr.experiment.workflows.Workflow.name).",
        ),
        sa.Column(
            "cache_key_prefix",
            sa.Text(),
            nullable=False,
            comment="The default cache key prefix for the steps this invocation schedules.",
        ),
        sa.Column(
            "returned_node",
            sa.LargeBinary(),
            nullable=True,
            comment="The identity of the node the body returned, once some attempt's body has returned; the result's value may still be unknown at that point (it depends on work that has not finished).",
        ),
        sa.Column(
            "result_cid",
            sa.LargeBinary(),
            nullable=True,
            comment="The materialized result (an Array entity) once the returned node has a value. Set means the invocation is finished.",
        ),
        sa.Column(
            "job_attempt_id",
            sa.Uuid(),
            nullable=True,
            comment="The job attempt whose process produced the result (the owner of the attempt that materialized it), set with result_cid: which code produced it.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "finished_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When result_cid was set.",
        ),
        sa.CheckConstraint(
            "(result_cid IS NULL) = (job_attempt_id IS NULL)",
            name="workflow_invocations_result_provenance",
        ),
        sa.ForeignKeyConstraint(
            ["job_attempt_id"],
            ["job_attempt_provenance.id"],
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["result_cid"],
            ["entities.cid"],
        ),
        sa.PrimaryKeyConstraint("id"),
        comment="One logical intent to run a workflow at a pathname within a job. Owns its nodes, names, and result; attempts (execution state) are the calls that try to run it, at most one of them live at a time. Finished when result_cid is set. Who requested it (its parent invocation, run node, and joint key) is the spawns row pointing at it; the job's root has none.",
    )
    op.create_index(
        "workflow_invocations_job_pathname",
        "workflow_invocations",
        ["job_id", "pathname"],
        unique=True,
    )
    op.create_table(
        "attempts",
        sa.Column("id", sa.Uuid(), nullable=False, comment="The attempt id, minted by the host."),
        sa.Column(
            "kind",
            sa.Text(),
            nullable=False,
            comment="'workflow': a call of a workflow body on behalf of the invocation in invocation_id. 'step': a call of a step for the request in request_cid, saved under cache_key. The two kinds share this table because a job attempt's process runs both and its attempts are swept together when it is evicted.",
        ),
        sa.Column(
            "invocation_id",
            sa.Uuid(),
            nullable=True,
            comment="For kind 'workflow': the invocation the attempt runs. Null for steps.",
        ),
        sa.Column(
            "cache_key",
            sa.Text(),
            nullable=True,
            comment="For kind 'step': the cache key the attempt's result is saved under (see step_cache). Null for workflows.",
        ),
        sa.Column(
            "request_cid",
            sa.LargeBinary(),
            nullable=True,
            comment="For kind 'step': the step request this attempt executes (see step_requests); its cid is the call's fingerprint. Null for workflows.",
        ),
        sa.Column(
            "owner_job_attempt_id",
            sa.Uuid(),
            nullable=False,
            comment="The job attempt whose process runs this attempt: the lease it lives under. An attempt writes while it is running; evicting its job attempt abandons it. For a step attempt, the job attempt that requested the step and started running it; other jobs waiting on the same key wait on the attempt through the cache entry, not through ownership.",
        ),
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            comment="'running' from the moment the row exists (an attempt is inserted running, by its owner's process); then exactly one of: 'completed' (a workflow's body returned and its result was materialized; a step's is the step result with this attempt's id), 'failed' (user code or the runner raised, a determinism violation, or a failed node was observed or returned; see error), 'interrupted' (a child step or workflow it depended on failed; see reason), 'abandoned' (its process stopped it for reasons unrelated to the attempt, or its job attempt was evicted; see reason).",
        ),
        sa.Column(
            "error",
            postgresql.JSONB(astext_type=Text()),
            nullable=True,
            comment="For state 'failed': {type_name, message, traceback}.",
        ),
        sa.Column(
            "reason",
            sa.Text(),
            nullable=True,
            comment="For states 'interrupted' and 'abandoned': a human-readable explanation.",
        ),
        sa.Column(
            "stopped_on",
            postgresql.JSONB(astext_type=Text()),
            nullable=True,
            comment="For states 'failed' and 'interrupted', when a node's state is what stopped the attempt: the identities (as cid strings) of the nodes it stopped on, each the node where an error originated (whose own computation raised, or whose children failed), never a node the error merely propagated to. For a failure at observe, the observed node's origin; for a failure at return, every origin (no one of them is the cause); for an interruption, the run nodes whose children had failed. Null when user code raised, or the attempt was abandoned. What went wrong with those nodes is derived: a derived node's error by recomputing it from the leaves, a child's by its own attempts (via spawns).",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="When the attempt started running (the row was inserted).",
        ),
        sa.Column(
            "finished_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When it left 'running'.",
        ),
        sa.CheckConstraint(
            "(kind = 'workflow' AND invocation_id IS NOT NULL AND cache_key IS NULL AND request_cid IS NULL) OR (kind = 'step' AND invocation_id IS NULL AND cache_key IS NOT NULL AND request_cid IS NOT NULL)",
            name="attempts_kind_columns",
        ),
        sa.CheckConstraint("kind IN ('workflow', 'step')", name="attempts_kind"),
        sa.CheckConstraint(
            "state IN ('running', 'completed', 'failed', 'interrupted', 'abandoned')",
            name="attempts_state",
        ),
        sa.ForeignKeyConstraint(["invocation_id"], ["workflow_invocations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["owner_job_attempt_id"],
            ["job_attempts.id"],
        ),
        sa.ForeignKeyConstraint(
            ["request_cid"],
            ["step_requests.request_cid"],
        ),
        sa.PrimaryKeyConstraint("id"),
        comment="One call of a user function: a workflow body on behalf of an invocation (at most one running per invocation, enforced by attempts_one_live_per_invocation) or a step for a request under a cache key (see step_cache for which attempt is current for a key and job_step_bindings for the result each job's request got). Every attempt is owned by the job attempt whose process runs it. Closed attempts are history: what a workflow attempt did is in attempt_events; what a completed step attempt produced is the step result (step_results) with the attempt's id, the ledger's record of it.",
    )
    op.create_index("attempts_cache_key", "attempts", ["cache_key", "started_at"], unique=False)
    op.create_index(
        "attempts_invocation", "attempts", ["invocation_id", "started_at"], unique=False
    )
    op.create_index(
        "attempts_one_live_per_invocation",
        "attempts",
        ["invocation_id"],
        unique=True,
        postgresql_where=sa.text("state = 'running'"),
    )
    op.create_index(
        "attempts_owner_running",
        "attempts",
        ["owner_job_attempt_id"],
        unique=False,
        postgresql_where=sa.text("state = 'running'"),
    )
    op.create_table(
        "job_step_bindings",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("cache_key", sa.Text(), nullable=False, comment="The cache key."),
        sa.Column(
            "step_result_id",
            sa.Uuid(),
            nullable=False,
            comment="The step result the job's request at the key is bound to: its value and which code produced it.",
        ),
        sa.Column(
            "bound_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["step_result_id"],
            ["step_results.id"],
        ),
        sa.PrimaryKeyConstraint("job_id", "cache_key"),
        comment="Which step result each job's step request is bound to. Set once, by the job itself (its running attempt), when it takes the step result the cache entry at the key offers; never moved, so a job sees one value per key whatever later happens to the cache entry.",
    )
    op.create_table(
        "node_names",
        sa.Column("invocation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "name",
            sa.Text(),
            nullable=False,
            comment="The name the workflow body gave the node. Source arrays, run steps, and run child workflows share this one namespace within an invocation.",
        ),
        sa.Column(
            "identity",
            sa.LargeBinary(),
            nullable=False,
            comment="The node the name belongs to. A later attempt emitting this name for a different identity is a determinism violation.",
        ),
        sa.ForeignKeyConstraint(["invocation_id"], ["workflow_invocations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("invocation_id", "name"),
        comment="Maps each name a workflow body gave a node (the 'name' argument of add_source_array, run_step, and run_child_workflow) to that node's identity. One namespace per invocation: a name may appear once per invocation, and re-emitting it for a different node is a determinism violation.",
    )
    op.create_table(
        "nodes",
        sa.Column("invocation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "identity",
            sa.LargeBinary(),
            nullable=False,
            comment="The node's identity: an ordinary entity id, the cid the node (fxtr.runner.nodes) would have as a stored entity, computed with the entity codec. Those bytes are not written to the entities table; the node is stored relationally instead (kind, params, type here, its edges in node_inputs) so it can be indexed and walked in SQL, and the node can be rebuilt from these columns to recompute the same id. Content-derived, so the same operation on the same inputs is one row however many attempts emit it.",
        ),
        sa.Column(
            "kind",
            sa.Text(),
            nullable=False,
            comment="The node's kind (fxtr.runner.nodes.KINDS): input, source_array, combine, stack, query, run_step, run_child_workflow.",
        ),
        sa.Column(
            "params",
            postgresql.JSONB(astext_type=Text()),
            nullable=False,
            comment="The node's parameters as JSON: every field of the node except the input references (see node_inputs) and the entity tag; array references as cid strings; a run node's declared result type in the form of fxtr.runner.postgres.json_values.array_type_to_json.",
        ),
        sa.Column(
            "type",
            postgresql.JSONB(astext_type=Text()),
            nullable=False,
            comment="The node's array type (array_type_to_json), DERIVED from its contents by fxtr.runner.nodes.node_type and stored for convenience: a leaf's is the referenced array's, a derived operation's follows from its parameters and inputs, a run node's is in its params. Never a source of truth.",
        ),
        sa.Column(
            "value_cid",
            sa.LargeBinary(),
            nullable=True,
            comment="The node's value (an Array entity), set for the leaves: inputs and source arrays. Every other node's value is found again by whoever needs it: a derived node's, a query's included, is recomputed from its inputs, and a run node's is collected from the results of what it expanded into (spawns). No node's error is stored: an attempt that a node's failure stopped names the node in attempts.stopped_on.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["invocation_id"], ["workflow_invocations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["value_cid"],
            ["entities.cid"],
        ),
        sa.PrimaryKeyConstraint("invocation_id", "identity"),
        comment="The nodes of every invocation's DAG, by identity, the union over the invocation's attempts. Inputs are created with the invocation (kind 'input', value_cid set, the argument name in params); everything else is emitted by attempts. Edges live in node_inputs.",
    )
    op.create_table(
        "spawns",
        sa.Column(
            "invocation_id",
            sa.Uuid(),
            nullable=False,
            comment="The invocation whose run node was expanded.",
        ),
        sa.Column(
            "node_identity",
            sa.LargeBinary(),
            nullable=False,
            comment="The run node (a node of that invocation).",
        ),
        sa.Column(
            "key",
            postgresql.JSONB(astext_type=Text()),
            nullable=False,
            comment="The joint key this spawn is for: the mapped values then the replica values, as a JSON array; [] when the run node maps and replicates over nothing.",
        ),
        sa.Column(
            "child_invocation_id",
            sa.Uuid(),
            nullable=True,
            comment="For a run_child_workflow node: the workflow invocation created for this key.",
        ),
        sa.Column(
            "step_cache_key",
            sa.Text(),
            nullable=True,
            comment="For a run_step node: the cache key requested for this key; the job's request there (job_step_requests) says with which fingerprint, its binding (job_step_bindings) which result it got. Exactly one of child_invocation_id and step_cache_key is set.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(child_invocation_id IS NOT NULL) <> (step_cache_key IS NOT NULL)",
            name="spawns_one_target",
        ),
        sa.ForeignKeyConstraint(
            ["child_invocation_id"], ["workflow_invocations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["invocation_id"], ["workflow_invocations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("invocation_id", "node_identity", "key"),
        comment="What each run node expanded into: one row per joint key, pointing at the target requested for it: a child workflow invocation, or a step at a cache key. The one record of who requested what: a workflow invocation's parent is the row pointing at it, and a job's step request is made by the rows naming its key. Written by the parent's active attempt when the run node's inputs all have values, every key of the node in one transaction, and never changed; any row for a node means the node is expanded, so a later attempt of the parent does not expand it again (a node that expanded into nothing, its mapped key set being empty, leaves no row and is harmlessly expanded into nothing again). The run node's value is derived from the targets' results and never stored.",
    )
    op.create_index("spawns_child", "spawns", ["child_invocation_id"], unique=False)
    op.create_table(
        "attempt_events",
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column(
            "seq",
            sa.Integer(),
            nullable=False,
            comment="The event's position in the attempt's log, from 0.",
        ),
        sa.Column(
            "kind",
            sa.Text(),
            nullable=False,
            comment="'emit': the body emitted the node; 'observe': the body asked for the node's value, recorded when it asked, whether or not a value ever came (a failed node is named in attempts.stopped_on); 'return': the body returned the node.",
        ),
        sa.Column("identity", sa.LargeBinary(), nullable=False, comment="The node concerned."),
        sa.Column(
            "at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("kind IN ('emit', 'observe', 'return')", name="attempt_events_kind"),
        sa.ForeignKeyConstraint(["attempt_id"], ["attempts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("attempt_id", "seq"),
        comment="What each attempt's body did, in order: the nodes it emitted, the nodes it asked to observe, and the node it returned, as pointers into the invocation's nodes. Requests, not results: values are derivable from the leaves and are not repeated here. Execution state (who did what), but written by the ledger's recording functions alongside the facts they record for the acting attempt, and read by the determinism check at return (the invocation's non-input nodes minus this attempt's 'emit' events must be empty). The 'return' event's presence is how a body's returning is attributed to an attempt.",
    )
    op.create_table(
        "attempt_logs",
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column(
            "seq",
            sa.Integer(),
            nullable=False,
            comment="The line's position in the attempt's log, from 0.",
        ),
        sa.Column(
            "message", sa.Text(), nullable=False, comment="The free-form line the body recorded."
        ),
        sa.Column(
            "at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["attempt_id"], ["attempts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("attempt_id", "seq"),
        comment="Free-form log lines an attempt's body recorded through ctx.log, in order. Pure observability state that participates in no rule: nothing here is read by the ledger or the policy, and it may be dropped without affecting correctness. Kept per attempt and never deduplicated across a replay: a later attempt of the same invocation re-runs the body and writes its own lines under its own id. (A stopgap, to be replaced by an event-streaming mechanism.)",
    )
    op.create_table(
        "step_cache",
        sa.Column(
            "cache_key", sa.Text(), nullable=False, comment="The cache key (fxtr.runner.steps)."
        ),
        sa.Column(
            "request_cid",
            sa.LargeBinary(),
            nullable=False,
            comment="The step request (a StepRequest entity) that currently owns the key; its cid is the fingerprint every request at this key must equal while the key holds a step result or a running attempt.",
        ),
        sa.Column(
            "attempt_id",
            sa.Uuid(),
            nullable=True,
            comment="The current attempt for the key, until one completes: the one a job with an equal request waits on while it is running. Replaced when a job starts the step again because it failed or was abandoned (possibly with a different request, if nothing live owned the key). Bindings other jobs hold are never touched by these replacements.",
        ),
        sa.Column(
            "step_result_id",
            sa.Uuid(),
            nullable=True,
            comment="The step result the key offers, once its attempt completed (completing moves the entry from the attempt to its step result) or once it was adopted from a job's binding. An equal request takes it.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(attempt_id IS NULL) <> (step_result_id IS NULL)", name="step_cache_attempt_or_result"
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["attempts.id"],
        ),
        sa.ForeignKeyConstraint(
            ["request_cid"],
            ["step_requests.request_cid"],
        ),
        sa.ForeignKeyConstraint(
            ["step_result_id"],
            ["step_results.id"],
        ),
        sa.PrimaryKeyConstraint("cache_key"),
        comment="The step cache: the mutable global address at each cache key, and the one thing jobs coordinate through. Says which request currently owns the key and either which attempt is current for it or which step result it offers; a key breaks the symmetry of a nondeterministic step, so equal requests at different keys are different calls. The contents of a key can change: clearing it (deleting the row) lets a different request take it, and a retry replaces the attempt. Existing bindings are unaffected by either.",
    )


def downgrade() -> None:
    raise RuntimeError(
        "Revision 0001 creates the runner's tables; undoing it would delete every job they hold."
        " Drop the schema instead, if that is what is meant."
    )
