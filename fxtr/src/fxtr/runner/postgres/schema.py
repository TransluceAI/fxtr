"""The runner's database schema, declared with SQLAlchemy Core.

Every table of the runner's schema is registered on :data:`metadata`, so
:meth:`~fxtr.runner.postgres.database.Database.create_schema` can create them all at once. This
module and the operations that query these tables are the only places that know the SQL shape of
the runner's state.

This module is also the source of truth for what the state *means*. Every table and column below
says what it holds and the facts that are not obvious from its type, and the same text is written
into the database as table and column comments, so the schema and a database together describe
the system's state without reading code. The tables fall into three groups:

* **The entity store and the project arrays** (``entities``, ``project_array*``): content-addressed
  values, and the named, versioned arrays users edit and launches snapshot into job inputs.
* **The ledger** (``jobs``, ``workflow_invocations``, ``spawns``, ``nodes``, ``node_inputs``,
  ``node_names``, ``job_attempt_provenance``, ``step_requests``, ``step_results``,
  ``job_step_requests``, ``job_step_bindings``): what is true about an experiment. Monotonic
  facts, written once: what was asked for, what each invocation's body described and returned,
  which job attempts there were and which code each ran, which step results exist and which job
  attempt produced each, which step each job requested at each cache key and which step result it
  is bound to. Nothing here says which process ran what or how it is going. Derived values are
  never stored anywhere in it: they follow from the leaves, and are recomputed by whoever needs
  them. **A ledger row refers only to other ledger rows and to entities, never to the execution
  state**, so a job's facts together with the facts they refer to (the step results its bindings
  name, the job attempts those and its results name) and the entities all of those reach are a
  self-contained record of the job.
* **The execution state** (``job_attempts``, ``attempts``, ``attempt_events``, ``attempt_logs``,
  ``step_cache``): how the runner is trying to make those facts. Mutable, owned by the execution
  policy (:mod:`fxtr.runner.postgres.execution`): which job attempts are directing a job's work
  and who owns them (the lease); every
  call of a workflow body or a step and how it ended; what each attempt did (as pointers into the
  ledger); the free-form lines each attempt logged; and the step cache's addresses.

Facts spanning several tables:

* **Facts and episodes share ids.** That a job attempt existed and which code its process ran is
  a ledger fact (``job_attempt_provenance``); how it went, its lease and its state, is execution
  state (``job_attempts``, same id). Likewise a step result (``step_results``) has the id of the
  step attempt that produced it (``attempts``), whose row says how that run went. A database has
  the execution rows only for what it ran itself, so it may hold the facts without them, and job
  attempt facts without their job: the provenance of a result its jobs took, produced by a job it
  does not have.
* **Leases.** A job attempt is one process's episode of running a job. Its ``owner`` names the
  process and its ``heartbeat_at`` is refreshed by that process while it runs; a job attempt is
  *fresh* while it is not evicted and its heartbeat is recent, and *stale* otherwise. Staleness
  informs: a stale job attempt is presumed dead, which the viewer shows and which lets a resume of
  the job take it over (evicting it: every running attempt it owns is abandoned). Nothing else
  acts on it; a running attempt is live whatever its owner's heartbeat says. Nothing is queued in
  the database: the owning process keeps its queue in memory and derives it from these tables when
  it starts.
* **Live attempts.** An attempt is *live* while ``running``; it is inserted running, by the
  process that owns its job attempt (``owner_job_attempt_id``). At most one attempt of an
  invocation is live at a time (a partial unique index on ``attempts``). The running attempt is
  the only writer of the invocation's nodes, names, and events, and every write for an attempt is
  guarded by ``state = 'running'`` on the attempt; an evicted job attempt's attempts have been
  abandoned, so their writes touch zero rows.
* **Finished.** A workflow invocation is finished when ``result_cid`` is set. Its ``returned_node``
  may be set earlier, when the body returned but the result's value was not yet known.
* **Job attempts.** A job's work is directed by its *active* job attempt (at most one per job):
  whether an unfinished target counts as pending rather than stopped depends on the job having
  one. A job attempt is settled ``succeeded`` when the root's result is materialized, ``stopped``
  when no attempt of the job is live and the root is unfinished, and ``cancelled`` by a person;
  each settles exactly once. It *tries each of the job's invocations once, plus replacements for
  abandonment*: an invocation is due to run under a job attempt while it is unfinished, nothing is
  running it, and every attempt of it the job attempt owns is abandoned.
* **Steps.** A job's request at a cache key is a fact (``job_step_requests``: one request per
  key per job; a second, different request there is the body's fault). Jobs coordinate only
  through the cache entry (``step_cache``: which request owns the key, and either the attempt
  current for it or the step result it offers): an equal request waits on the current attempt
  while it runs, takes the step result once there is one (completing an attempt moves the entry to
  its step result), or runs the step itself when the attempt failed or was abandoned; a different
  request finds a *cache conflict* at the key while it offers a step result or its attempt runs.
  If the job's active job attempt overwrites cache conflicts
  (``job_attempts.overwrite_cache_conflicts``), a different request's step result is not a
  conflict: the job runs its request and takes the entry. A job takes a step result by writing its
  own binding (``job_step_bindings``), a fact set once by the job itself and by nobody else: a job
  sees one value per key whatever later happens to the cache entry, and is done with the step
  only once its binding exists.
* **Job and invocation status is derived, never stored.** Per invocation: *completed* if finished;
  *running* if it has a live attempt; *queued* if its job's active job attempt is due to run it;
  otherwise, by its most recent attempt, *failed* (state ``failed``, with ``error``),
  *interrupted* (state ``interrupted``, with ``reason``), or *stopped* (incomplete and idle, with
  no attempt under the current episode to explain why: nothing will run it until a resume). The
  job is *succeeded* when the root invocation is completed, *running* when any of its invocations
  is queued or running, and otherwise *stopped*, a job nothing has taken included; "failed" is a
  stopped job with a failed invocation.
* **Nodes are content-addressed.** A node's ``identity`` is the cid it would have as a stored entity
  (:mod:`fxtr.runner.nodes`), so it is the same across attempts of the invocation and across
  invocations that emit the same operation on the same inputs. ``nodes.type`` is derived from
  the node's contents and stored for convenience; it is never a source of truth.
* **Determinism.** Later attempts of an invocation replay the same body against the same ledger.
  An emitted identity already in ``nodes`` is a match; a name in ``node_names`` mapped to a
  different identity is a violation; at return, every non-input node of the invocation must have
  been emitted by the returning attempt (``attempt_events`` with kind ``emit``), so a body that
  emits less than a previous attempt is a violation while one that emits more (it got further) is
  fine.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()
"""The registry of the runner's tables."""

entities = Table(
    "entities",
    metadata,
    Column("cid", LargeBinary, primary_key=True),
    Column("type_tag", Text, nullable=False),
    Column("data", LargeBinary, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
"""The entity store: one row per entity, keyed by its content id, holding its DRISL bytes.

``type_tag`` is the entity's ``$type`` tag (its type id string), so the kinds of entities in a
database can be inspected from SQL. Rows are only ever inserted; identical content is a no-op.
"""

project_arrays = Table(
    "project_arrays",
    metadata,
    Column("name", Text, primary_key=True),
    Column("type", JSONB, nullable=False),
    Column("version", BigInteger, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
"""The registry of project arrays: each has a declared array type (as JSON, see
:func:`fxtr.runner.postgres.json_values.array_type_to_json`) and a ``version`` that every
mutation of its rows increments. Updating the row also serializes concurrent mutations of one
array, since they all lock it first."""

project_array_rows = Table(
    "project_array_rows",
    metadata,
    Column(
        "array_name",
        Text,
        ForeignKey("project_arrays.name", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("key", JSONB, primary_key=True),
    Column("value", JSONB, nullable=False),
)
"""The current rows of every project array. ``key`` is the row's key values as a JSON array in
the declared dim order; ``value`` is the row's value encoded by the declared schema (see
:func:`fxtr.runner.postgres.json_values.value_to_json`). Dropping an array drops its rows."""

project_array_snapshots = Table(
    "project_array_snapshots",
    metadata,
    Column(
        "array_name",
        Text,
        ForeignKey("project_arrays.name", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("version", BigInteger, primary_key=True),
    Column("cid", LargeBinary, ForeignKey("entities.cid"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
"""Snapshots of project arrays: the entity (a :class:`~fxtr.entity_defns.array.Array`) holding an
array's rows as of one version. At most one per version. Dropping an array drops these records;
the entities remain."""


jobs = Table(
    "jobs",
    metadata,
    Column("id", Uuid, primary_key=True, comment="The job id, minted by the creator."),
    Column(
        "workflow_name",
        Text,
        nullable=False,
        comment="The root workflow's name (fxtr.experiment.workflows.Workflow.name).",
    ),
    Column(
        "root",
        Text,
        nullable=False,
        comment="The root invocation's pathname and default cache key prefix (the job's 'root'"
        " argument; 'root' by default). Pathnames are scoped to the job, cache keys are shared"
        " across jobs, so jobs of different root workflows sharing one step cache pick distinct"
        " roots.",
    ),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    comment="One request to execute a root workflow with given inputs. Its state is derived from"
    " its invocations and attempts, never stored (see the module docstring); how it is being"
    " executed is the job_attempts' business.",
)
"""One request to execute a root workflow. See the column comments."""

workflow_invocations = Table(
    "workflow_invocations",
    metadata,
    Column("id", Uuid, primary_key=True, comment="The invocation id, minted by the creator."),
    Column("job_id", Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
    Column(
        "pathname",
        Text,
        nullable=False,
        comment="The invocation's path in the experiment (fxtr.experiment.workflows.join_path /"
        " append_mapped_key), unique within the job: the root's is the job's root; a child's is"
        " its parent's pathname, the run node's name, and the mapped key.",
    ),
    Column(
        "workflow_name",
        Text,
        nullable=False,
        comment="The workflow to run (fxtr.experiment.workflows.Workflow.name).",
    ),
    Column(
        "cache_key_prefix",
        Text,
        nullable=False,
        comment="The default cache key prefix for the steps this invocation schedules.",
    ),
    Column(
        "returned_node",
        LargeBinary,
        nullable=True,
        comment="The identity of the node the body returned, once some attempt's body has"
        " returned; the result's value may still be unknown at that point (it depends on work"
        " that has not finished).",
    ),
    Column(
        "result_cid",
        LargeBinary,
        ForeignKey("entities.cid"),
        nullable=True,
        comment="The materialized result (an Array entity) once the returned node has a value."
        " Set means the invocation is finished.",
    ),
    Column(
        "job_attempt_id",
        Uuid,
        ForeignKey("job_attempt_provenance.id"),
        nullable=True,
        comment="The job attempt whose process produced the result (the owner of the attempt that"
        " materialized it), set with result_cid: which code produced it.",
    ),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column(
        "finished_at",
        DateTime(timezone=True),
        nullable=True,
        comment="When result_cid was set.",
    ),
    CheckConstraint(
        "(result_cid IS NULL) = (job_attempt_id IS NULL)",
        name="workflow_invocations_result_provenance",
    ),
    Index("workflow_invocations_job_pathname", "job_id", "pathname", unique=True),
    comment="One logical intent to run a workflow at a pathname within a job. Owns its nodes,"
    " names, and result; attempts (execution state) are the calls that try to run it, at most"
    " one of them live at a time. Finished when result_cid is set. Who requested it (its parent"
    " invocation, run node, and joint key) is the spawns row pointing at it; the job's root has"
    " none.",
)
"""One logical invocation of a workflow within a job. See the column comments."""

spawns = Table(
    "spawns",
    metadata,
    Column(
        "invocation_id",
        Uuid,
        ForeignKey("workflow_invocations.id", ondelete="CASCADE"),
        primary_key=True,
        comment="The invocation whose run node was expanded.",
    ),
    Column(
        "node_identity",
        LargeBinary,
        primary_key=True,
        comment="The run node (a node of that invocation).",
    ),
    Column(
        "key",
        JSONB,
        primary_key=True,
        comment="The joint key this spawn is for: the mapped values then the replica values, as a"
        " JSON array; [] when the run node maps and replicates over nothing.",
    ),
    Column(
        "child_invocation_id",
        Uuid,
        ForeignKey("workflow_invocations.id", ondelete="CASCADE"),
        nullable=True,
        comment="For a run_child_workflow node: the workflow invocation created for this key.",
    ),
    Column(
        "step_cache_key",
        Text,
        nullable=True,
        comment="For a run_step node: the cache key requested for this key; the job's request"
        " there (job_step_requests) says with which fingerprint, its binding (job_step_bindings)"
        " which result it got. Exactly one of child_invocation_id and step_cache_key is set.",
    ),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "(child_invocation_id IS NOT NULL) <> (step_cache_key IS NOT NULL)",
        name="spawns_one_target",
    ),
    Index("spawns_child", "child_invocation_id"),
    comment="What each run node expanded into: one row per joint key, pointing at the target"
    " requested for it: a child workflow invocation, or a step at a cache key. The one record of"
    " who requested what: a workflow invocation's parent is the row pointing at it, and a job's"
    " step request is made by the rows naming its key. Written by the parent's active attempt"
    " when the run node's inputs all have values, every key of the node in one transaction, and"
    " never changed; any row for a node means the node is expanded, so a later attempt of the"
    " parent does not expand it again (a node that expanded into nothing, its mapped key set"
    " being empty, leaves no row and is harmlessly expanded into nothing again). The run node's"
    " value is derived from the targets' results and never stored.",
)
"""What each run node expanded into. See the column comments."""

nodes = Table(
    "nodes",
    metadata,
    Column(
        "invocation_id",
        Uuid,
        ForeignKey("workflow_invocations.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "identity",
        LargeBinary,
        primary_key=True,
        comment="The node's identity: an ordinary entity id, the cid the node (fxtr.runner.nodes)"
        " would have as a stored entity, computed with the entity codec. Those bytes are not"
        " written to the entities table; the node is stored relationally instead (kind, params,"
        " type here, its edges in node_inputs) so it can be indexed and walked in SQL, and the node"
        " can be rebuilt from these columns to recompute the same id. Content-derived, so the same"
        " operation on the same inputs is one row however many attempts emit it.",
    ),
    Column(
        "kind",
        Text,
        nullable=False,
        comment="The node's kind (fxtr.runner.nodes.KINDS): input, source_array, combine,"
        " stack, query, run_step, run_child_workflow.",
    ),
    Column(
        "params",
        JSONB,
        nullable=False,
        comment="The node's parameters as JSON: every field of the node except the"
        " input references (see node_inputs) and the entity tag; array references as cid"
        " strings; a run node's declared result type in the form of"
        " fxtr.runner.postgres.json_values.array_type_to_json.",
    ),
    Column(
        "type",
        JSONB,
        nullable=False,
        comment="The node's array type (array_type_to_json), DERIVED from its contents by"
        " fxtr.runner.nodes.node_type and stored for convenience: a leaf's is the referenced"
        " array's, a derived operation's follows from its parameters and inputs, a run node's is"
        " in its params. Never a source of truth.",
    ),
    Column(
        "value_cid",
        LargeBinary,
        ForeignKey("entities.cid"),
        nullable=True,
        comment="The node's value (an Array entity), set for the leaves: inputs and source"
        " arrays. Every other node's value is found again by whoever needs it: a derived node's,"
        " a query's included, is recomputed from its inputs, and a run node's is collected from"
        " the results of what it expanded into (spawns). No node's error is stored: an attempt"
        " that a node's failure stopped names the node in attempts.stopped_on.",
    ),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    comment="The nodes of every invocation's DAG, by identity, the union over the invocation's"
    " attempts. Inputs are created with the invocation (kind 'input', value_cid set, the"
    " argument name in params); everything else is emitted by attempts. Edges live in"
    " node_inputs.",
)
"""The nodes of every invocation's DAG. See the column comments."""

node_inputs = Table(
    "node_inputs",
    metadata,
    Column("invocation_id", Uuid, primary_key=True),
    Column("node_identity", LargeBinary, primary_key=True),
    Column(
        "label",
        JSONB,
        primary_key=True,
        comment="How the node knows this input: an argument name (combine, run nodes), a key"
        " value along the new dim (stack; an int or a str), or the position the query plan's"
        " QuerySource reads it at (query; an int). One query node"
        " may read the same input at several positions, so these rows are not one per input.",
    ),
    Column(
        "input_identity",
        LargeBinary,
        nullable=False,
        comment="The identity of the input node (a node of the same invocation).",
    ),
    comment="The edges of every invocation's DAG: one row per input of a node, with the label the"
    " node knows it by. Together with nodes.params this is the whole node.",
)
"""The edges of every invocation's DAG. See the column comments."""

node_names = Table(
    "node_names",
    metadata,
    Column(
        "invocation_id",
        Uuid,
        ForeignKey("workflow_invocations.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "name",
        Text,
        primary_key=True,
        comment="The name the workflow body gave the node. Source arrays, run steps, and run"
        " child workflows share this one namespace within an invocation.",
    ),
    Column(
        "identity",
        LargeBinary,
        nullable=False,
        comment="The node the name belongs to. A later attempt emitting this name for a different"
        " identity is a determinism violation.",
    ),
    comment="Maps each name a workflow body gave a node (the 'name' argument of add_source_array,"
    " run_step, and run_child_workflow) to that node's identity. One namespace per invocation:"
    " a name may appear once per invocation, and re-emitting it for a different node is a"
    " determinism violation.",
)
"""The named nodes of each invocation, by name. See the column comments."""

job_attempt_provenance = Table(
    "job_attempt_provenance",
    metadata,
    Column(
        "id",
        Uuid,
        primary_key=True,
        comment="The job attempt's id: the same as its job_attempts row, where this database ran"
        " it.",
    ),
    Column(
        "job_id",
        Uuid,
        nullable=False,
        comment="The job the job attempt ran. Deliberately not a foreign key: this database may"
        " hold the fact without the job, when it arrived with a result another job took from the"
        " cache (see the module docstring).",
    ),
    Column(
        "code_version_cid",
        LargeBinary,
        ForeignKey("entities.cid"),
        nullable=False,
        comment="The code the job attempt's process ran: a CodeVersion entity"
        " (fxtr.project.code_version): the commit its checkout pinned when it launched or resumed"
        " the job, the project within the repository, whether the environment was checked, and"
        " where each function it could run is implemented. Every attempt the job attempt owned ran"
        " at it. The job's launch version is its first job attempt's; a later job attempt may run"
        " at another commit and says so here.",
    ),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="When the job attempt was created: the wall clock at the insert, not the start of"
        " its transaction, so a job's job attempts are in creation order by it (ties broken by"
        " id). Their positions among the job's (1, 2, ...) are counted from it when read.",
    ),
    UniqueConstraint("id", "job_id", name="job_attempt_provenance_id_job"),
    Index("job_attempt_provenance_job_created", "job_id", "created_at", "id"),
    comment="That a job attempt existed and which code its process ran: the immutable fact about"
    " a job attempt, written with it and never changed. Results point here for their provenance."
    " How the job attempt went (its lease, its state) is execution state, in job_attempts, which"
    " exists only for job attempts this database ran.",
)
"""That a job attempt existed and which code it ran. See the column comments."""

# ---- the execution state: attempts, their events, the step cache and subscriptions ----

attempts = Table(
    "attempts",
    metadata,
    Column("id", Uuid, primary_key=True, comment="The attempt id, minted by the host."),
    Column(
        "kind",
        Text,
        nullable=False,
        comment="'workflow': a call of a workflow body on behalf of the invocation in"
        " invocation_id. 'step': a call of a step for the request in request_cid, saved under"
        " cache_key. The two kinds share this table because a job attempt's process runs both"
        " and its attempts are swept together when it is evicted.",
    ),
    Column(
        "invocation_id",
        Uuid,
        ForeignKey("workflow_invocations.id", ondelete="CASCADE"),
        nullable=True,
        comment="For kind 'workflow': the invocation the attempt runs. Null for steps.",
    ),
    Column(
        "cache_key",
        Text,
        nullable=True,
        comment="For kind 'step': the cache key the attempt's result is saved under (see"
        " step_cache). Null for workflows.",
    ),
    Column(
        "request_cid",
        LargeBinary,
        ForeignKey("step_requests.request_cid"),
        nullable=True,
        comment="For kind 'step': the step request this attempt executes (see step_requests);"
        " its cid is the call's fingerprint. Null for workflows.",
    ),
    Column(
        "owner_job_attempt_id",
        Uuid,
        ForeignKey("job_attempts.id"),
        nullable=False,
        comment="The job attempt whose process runs this attempt: the lease it lives under. An"
        " attempt writes while it is running; evicting its job attempt abandons it. For a step"
        " attempt, the job attempt that requested the step and started running it; other jobs"
        " waiting on the same key wait on the attempt through the cache entry, not through"
        " ownership.",
    ),
    Column(
        "state",
        Text,
        nullable=False,
        comment="'running' from the moment the row exists (an attempt is inserted running, by its"
        " owner's process); then exactly one of: 'completed' (a workflow's body returned and its"
        " result was materialized; a step's is the step result with this attempt's id), 'failed'"
        " (user code or the runner raised, a determinism violation, or a failed node was observed"
        " or returned; see error), 'interrupted' (a child step or workflow it depended on failed;"
        " see reason), 'abandoned' (its process stopped it for reasons unrelated to the attempt,"
        " or its job attempt was evicted; see reason).",
    ),
    Column(
        "error",
        JSONB,
        nullable=True,
        comment="For state 'failed': {type_name, message, traceback}.",
    ),
    Column(
        "reason",
        Text,
        nullable=True,
        comment="For states 'interrupted' and 'abandoned': a human-readable explanation.",
    ),
    Column(
        "stopped_on",
        JSONB,
        nullable=True,
        comment="For states 'failed' and 'interrupted', when a node's state is what stopped the"
        " attempt: the identities (as cid strings) of the nodes it stopped on, each the node where"
        " an error originated (whose own computation raised, or whose children failed), never a"
        " node the error merely propagated to. For a failure at observe, the observed node's"
        " origin; for a failure at return, every origin (no one of them is the cause); for an"
        " interruption, the run nodes whose children had failed. Null when user code raised, or"
        " the attempt was abandoned. What went wrong with those nodes is derived: a derived node's"
        " error by recomputing it from the leaves, a child's by its own attempts (via spawns).",
    ),
    Column(
        "started_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="When the attempt started running (the row was inserted).",
    ),
    Column(
        "finished_at", DateTime(timezone=True), nullable=True, comment="When it left 'running'."
    ),
    CheckConstraint("kind IN ('workflow', 'step')", name="attempts_kind"),
    CheckConstraint(
        "state IN ('running', 'completed', 'failed', 'interrupted', 'abandoned')",
        name="attempts_state",
    ),
    CheckConstraint(
        "(kind = 'workflow' AND invocation_id IS NOT NULL AND cache_key IS NULL"
        " AND request_cid IS NULL)"
        " OR (kind = 'step' AND invocation_id IS NULL AND cache_key IS NOT NULL"
        " AND request_cid IS NOT NULL)",
        name="attempts_kind_columns",
    ),
    Index("attempts_invocation", "invocation_id", "started_at"),
    Index("attempts_cache_key", "cache_key", "started_at"),
    Index(
        "attempts_owner_running",
        "owner_job_attempt_id",
        postgresql_where=text("state = 'running'"),
    ),
    Index(
        "attempts_one_live_per_invocation",
        "invocation_id",
        unique=True,
        postgresql_where=text("state = 'running'"),
    ),
    comment="One call of a user function: a workflow body on behalf of an invocation (at most one"
    " running per invocation, enforced by attempts_one_live_per_invocation) or a step for a"
    " request under a cache key (see step_cache for which attempt is current for a key and"
    " job_step_bindings for the result each job's request got). Every attempt is owned by the"
    " job attempt whose process runs it. Closed attempts are history: what a workflow attempt did"
    " is in attempt_events; what a completed step attempt produced is the step result"
    " (step_results) with the attempt's id, the ledger's record of it.",
)
"""One call of a user function. See the column comments."""

attempt_events = Table(
    "attempt_events",
    metadata,
    Column("attempt_id", Uuid, ForeignKey("attempts.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "seq",
        Integer,
        primary_key=True,
        comment="The event's position in the attempt's log, from 0.",
    ),
    Column(
        "kind",
        Text,
        nullable=False,
        comment="'emit': the body emitted the node; 'observe': the body"
        " asked for the node's value, recorded when it asked, whether or not a value ever came"
        " (a failed node is named in attempts.stopped_on); 'return': the body returned"
        " the node.",
    ),
    Column("identity", LargeBinary, nullable=False, comment="The node concerned."),
    Column("at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("kind IN ('emit', 'observe', 'return')", name="attempt_events_kind"),
    comment="What each attempt's body did, in order: the nodes it emitted, the nodes it asked to"
    " observe, and the node it returned, as pointers into the invocation's nodes. Requests, not"
    " results: values are derivable from the leaves and are not repeated here. Execution state"
    " (who did what), but written by the ledger's recording functions alongside the facts they"
    " record for the acting attempt, and read by the determinism check at return (the"
    " invocation's non-input nodes minus this attempt's 'emit' events must be empty). The"
    " 'return' event's presence is how a body's returning is attributed to an attempt.",
)
"""Each attempt's log. See the column comments."""

attempt_logs = Table(
    "attempt_logs",
    metadata,
    Column("attempt_id", Uuid, ForeignKey("attempts.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "seq",
        Integer,
        primary_key=True,
        comment="The line's position in the attempt's log, from 0.",
    ),
    Column("message", Text, nullable=False, comment="The free-form line the body recorded."),
    Column("at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    comment="Free-form log lines an attempt's body recorded through ctx.log, in order. Pure"
    " observability state that participates in no rule: nothing here is read by the ledger or the"
    " policy, and it may be dropped without affecting correctness. Kept per attempt and never"
    " deduplicated across a replay: a later attempt of the same invocation re-runs the body and"
    " writes its own lines under its own id. (A stopgap, to be replaced by an event-streaming"
    " mechanism.)",
)
"""Each attempt's free-form log lines. See the column comments."""


step_requests = Table(
    "step_requests",
    metadata,
    Column(
        "request_cid",
        LargeBinary,
        ForeignKey("entities.cid"),
        primary_key=True,
        comment="The request's fingerprint: the cid of the StepRequest entity (fxtr.runner.steps)"
        " in the entities table, which holds the whole call including the input arrays.",
    ),
    Column(
        "step_name",
        Text,
        nullable=False,
        comment="The step the request calls, as the entity records it.",
    ),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    comment="The relational face of every step request the system has seen: the fields of the"
    " request entity a query needs (claim matches a worker's StepSpec on step_name), keyed by"
    " the fingerprint, the way nodes are stored as rows beside"
    " their identity. One row per distinct call, written when the call is first requested and"
    " never changed; attempts and cache entries reference it.",
)
"""The queryable fields of every step request, by fingerprint. See the column comments."""

job_step_requests = Table(
    "job_step_requests",
    metadata,
    Column("job_id", Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True),
    Column("cache_key", Text, primary_key=True, comment="The cache key requested."),
    Column(
        "request_cid",
        LargeBinary,
        ForeignKey("step_requests.request_cid"),
        nullable=False,
        comment="The request (its fingerprint) the job made at the key. One per key per job: a"
        " body asking for a different request at a key its job already requested used one key"
        " for two configurations, a failure of the requesting attempt.",
    ),
    Column("requested_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    comment="What each job asked for at each cache key: a fact about the workflow, written once by"
    " the first attempt that requests the key and matched by every later one (replay).",
)
"""Each job's request per cache key. See the column comments."""

step_results = Table(
    "step_results",
    metadata,
    Column(
        "id",
        Uuid,
        primary_key=True,
        comment="The step result's id: the id of the step attempt that produced it, where this"
        " database ran that attempt (attempts.id). Deliberately not a foreign key: a database may"
        " hold a step result without the attempt, when the result came from elsewhere.",
    ),
    Column(
        "request_cid",
        LargeBinary,
        ForeignKey("step_requests.request_cid"),
        nullable=False,
        comment="The step request that was run (its fingerprint).",
    ),
    Column(
        "result_cid",
        LargeBinary,
        ForeignKey("entities.cid"),
        nullable=False,
        comment="What it produced (an Array entity).",
    ),
    Column(
        "job_attempt_id",
        Uuid,
        ForeignKey("job_attempt_provenance.id"),
        nullable=False,
        comment="The job attempt whose process ran it (the producing attempt's owner): which code"
        " produced the result. Another job's, for a result jobs took from the cache.",
    ),
    Column(
        "completed_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="When the producing attempt completed.",
    ),
    comment="Every completed run of a step: the fact that a request, run under a job attempt,"
    " produced a result. Written once, when the step attempt completes, and never changed; not"
    " keyed by job or cache key, since jobs share results through the cache. Bindings and"
    " completed cache entries point here.",
)
"""Every completed run of a step. See the column comments."""

job_step_bindings = Table(
    "job_step_bindings",
    metadata,
    Column("job_id", Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True),
    Column("cache_key", Text, primary_key=True, comment="The cache key."),
    Column(
        "step_result_id",
        Uuid,
        ForeignKey("step_results.id"),
        nullable=False,
        comment="The step result the job's request at the key is bound to: its value and which"
        " code produced it.",
    ),
    Column("bound_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    comment="Which step result each job's step request is bound to. Set once, by the job itself"
    " (its running attempt), when it takes the step result the cache entry at the key offers;"
    " never moved, so a job sees one value per key whatever later happens to the cache entry.",
)
"""Each job's step result per cache key. See the column comments."""

step_cache = Table(
    "step_cache",
    metadata,
    Column("cache_key", Text, primary_key=True, comment="The cache key (fxtr.runner.steps)."),
    Column(
        "request_cid",
        LargeBinary,
        ForeignKey("step_requests.request_cid"),
        nullable=False,
        comment="The step request (a StepRequest entity) that currently owns the key; its cid is"
        " the fingerprint every request at this key must equal while the key holds a step result"
        " or a running attempt.",
    ),
    Column(
        "attempt_id",
        Uuid,
        ForeignKey("attempts.id"),
        nullable=True,
        comment="The current attempt for the key, until one completes: the one a job with an"
        " equal request waits on while it is running. Replaced when a job starts the step again"
        " because it failed or was abandoned (possibly with a different request, if nothing live"
        " owned the key). Bindings other jobs hold are never touched by these replacements.",
    ),
    Column(
        "step_result_id",
        Uuid,
        ForeignKey("step_results.id"),
        nullable=True,
        comment="The step result the key offers, once its attempt completed (completing moves the"
        " entry from the attempt to its step result) or once it was adopted from a job's binding."
        " An equal request takes it.",
    ),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "(attempt_id IS NULL) <> (step_result_id IS NULL)", name="step_cache_attempt_or_result"
    ),
    comment="The step cache: the mutable global address at each cache key, and the one thing"
    " jobs coordinate through. Says which request currently owns the key and either which"
    " attempt is current for it or which step result it offers; a key breaks the symmetry of a"
    " nondeterministic step, so equal requests at different keys are different calls. The"
    " contents of a key can change: clearing it (deleting the row) lets a different request take"
    " it, and a retry replaces the attempt. Existing bindings are unaffected by either.",
)
"""The step cache: the current request at each cache key, and its attempt or step result. See the
column comments."""

# ---- the execution state: job attempts ----

job_attempts = Table(
    "job_attempts",
    metadata,
    Column(
        "id",
        Uuid,
        primary_key=True,
        comment="The job attempt id, minted by the creator; its fact (which code it ran, when it"
        " was created) is the job_attempt_provenance row with the same id and job.",
    ),
    Column("job_id", Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
    Column(
        "state",
        Text,
        nullable=False,
        comment="'active': directing the job's work (at most one per job, enforced by"
        " job_attempts_one_active). Then exactly one of: 'succeeded' (the root's result was"
        " materialized while it was active), 'stopped' (no attempt of the job was live and the"
        " root unfinished: everything it scheduled has failed, been interrupted, or was never"
        " replaced), 'cancelled' (by a person; see reason). A settled job attempt never returns"
        " to active; resuming creates a new one.",
    ),
    Column(
        "reason",
        Text,
        nullable=True,
        comment="For state 'cancelled': why, as the person gave it.",
    ),
    Column(
        "owner",
        Text,
        nullable=False,
        comment="The process that runs this job attempt (hostname, pid, and a random token;"
        " display only). Together with heartbeat_at, the lease.",
    ),
    Column(
        "heartbeat_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="When the owner last refreshed its lease. The job attempt is fresh while it is"
        " active, not evicted, and this is recent (the execution policy's staleness threshold);"
        " stale otherwise, when its process is presumed dead and a resume may take it over.",
    ),
    Column(
        "evicted_at",
        DateTime(timezone=True),
        nullable=True,
        comment="Set when another process took the job attempt over or a hard cancel stopped it:"
        " every attempt it owned was abandoned, and its process finds out on its next heartbeat."
        " Distinct from settling: an evicted job attempt is also settled (stopped or cancelled).",
    ),
    Column("evicted_reason", Text, nullable=True, comment="For evicted_at: why."),
    Column(
        "overwrite_cache_conflicts",
        Boolean,
        nullable=False,
        server_default=false(),
        comment="Whether this job attempt overwrites its cache conflicts with completed results:"
        " while it is active, a request of its job at a key that offers a different request's"
        " step result runs, and takes the entry. A conflict with a running attempt is not"
        " overwritten. Set when the job attempt is created, by the launch or resume that asked"
        " for it; a later job attempt does not inherit it.",
    ),
    Column(
        "settled_at",
        DateTime(timezone=True),
        nullable=True,
        comment="When it left 'active'.",
    ),
    CheckConstraint(
        "state IN ('active', 'succeeded', 'stopped', 'cancelled')", name="job_attempts_state"
    ),
    ForeignKeyConstraint(
        ["id", "job_id"],
        ["job_attempt_provenance.id", "job_attempt_provenance.job_id"],
        name="job_attempts_provenance",
    ),
    Index(
        "job_attempts_one_active",
        "job_id",
        unique=True,
        postgresql_where=text("state = 'active'"),
    ),
    comment="One process's episode of running a job, as this database runs it: the scheduling"
    " policy's state for it and the lease its owner holds (which code it runs, and when it was"
    " created, are its job_attempt_provenance fact). The active job attempt decides what is due"
    " to run and whether an unfinished target is pending or stopped, and owns every attempt its"
    " process runs (attempts.owner_job_attempt_id). Settling it (succeeded, stopped, cancelled)"
    " means it stops deciding; evicting it (evicted_at) abandons what it owns.",
)
"""One episode of trying to finish a job. See the column comments."""
