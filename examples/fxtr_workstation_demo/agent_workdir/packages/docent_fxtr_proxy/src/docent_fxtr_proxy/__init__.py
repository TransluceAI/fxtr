"""Agent-side typed view of the demo's docent-proxy service.

The demo backend registers a service under the well-known external
name ``"docent-fxtr-proxy"``. It exposes a small read-only slice of
the Docent SDK as typed-entity-producing methods so the agent can
query a Docent collection without needing the Docent SDK installed
inside the sandbox. Each call returns an :class:`EntityRef` to one
of the typed entity dataclasses declared in this module; the agent
``.load()``\\ s the ref to get a structured value back.

The methods exposed:

- :meth:`DocentProto.execute_dql` — run a DQL query string against a
  collection and get rows back. DQL is a read-only subset of SQL.
- :meth:`DocentProto.get_dql_schema` — describe the tables / columns
  available to DQL for a collection.
- :meth:`DocentProto.get_metadata_fields` — list the metadata fields
  that have actually been logged on agent runs in a collection
  (optionally with sample values).

Typical use — DQL with the convenience helper:

.. code-block:: python

    import docent_fxtr_proxy as docent
    from _fxtr_mcp_builtin import retrieve_result

    collection_id = "<paste-collection-uuid-here>"
    await docent.schedule_execute_dql(
        "rows",
        collection_id=collection_id,
        query='''
            SELECT id, metadata_json->>'task' AS task
            FROM agent_runs
            LIMIT 25
        ''',
    )
    # In a follow-up run_script (or with wait_seconds on the
    # enclosing run_script):
    result_ref = await retrieve_result("rows", EntityRef[docent.DqlResult])
    result = await result_ref.load()
    for row in docent.dql_result_to_dicts(result):
        print(row["task"])

Workflow tips (applies whether you're doing analysis, building a
visualization, extracting data for downstream code, etc.):

- **Don't assume the metadata shape.** Per-collection metadata
  varies. Call :func:`schedule_get_metadata_fields` first if you're
  about to write a DQL query that touches ``metadata_json`` —
  passing ``include_sample_values=True`` is often worth it because
  the field types in DQL are JSON and sample values disambiguate
  what's actually inside (string-of-number vs. number,
  enum-of-strings, nested objects, etc.).
- **Use :func:`schedule_get_dql_schema` when you're unsure what
  tables / columns are available.** The five tables in the
  read-side schema are documented under :class:`DocentProto` below,
  but the schema call returns the live shape for the collection
  you're querying.
- **Iterate small.** DQL is capped at 10,000 rows per query — use
  ``LIMIT`` while exploring, then expand once you know the query
  shape is right. Result-set construction (LLM-side analysis) is
  out of scope for this proxy; this service is read-only.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any, Protocol, cast

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FrozenFxtrCbor, FxtrID
from fxtr2.data.entity_ref_scope import EntityRef
from fxtr2.services.typed_service import service_protocol

#: External name the docent-proxy service is registered under.
#: Wire-stable; don't change.
DOCENT_PROXY_EXTERNAL_NAME = "docent-fxtr-proxy"

#: Stable namespace UUID for this proxy's entity type-ids. Wire-stable;
#: don't change. Generated once for this package and held fixed so a
#: type's wire identity stays put even as field shapes evolve.
_DOCENT_PROXY_NS = uuid.UUID("9b3c7a14-2d8e-4f51-91d6-7a4b5c6d8e2f")

DQL_RESULT_TYPE_ID: FxtrID = system_entities.define_type(_DOCENT_PROXY_NS, "DqlResult")
DQL_SCHEMA_TYPE_ID: FxtrID = system_entities.define_type(_DOCENT_PROXY_NS, "DqlSchema")
METADATA_FIELDS_TYPE_ID: FxtrID = system_entities.define_type(_DOCENT_PROXY_NS, "MetadataFields")


@dataclasses.dataclass(frozen=True)
class DqlResult(DataclassEntity, fxtr_type=DQL_RESULT_TYPE_ID):
    """Rows returned by a single :meth:`DocentProto.execute_dql` call.

    The shape mirrors the raw Docent API response (``columns`` +
    ``rows``) — :func:`dql_result_to_dicts` turns it into a list of
    dict-per-row if that's more convenient.

    Attributes:
        collection_id: The collection the query ran against.
        query: The exact DQL query string that was executed (handy
            for tracing / re-running).
        columns: Column names in the order they appear in each row.
        rows: Tuple of rows; each row is a tuple of cell values.
            Cell values are whatever Postgres returned (strings,
            numbers, booleans, ``None``, nested JSON objects when a
            column projects a JSON expression). Cell typing is
            preserved via :data:`FrozenFxtrCbor` — for nested JSON,
            you'll see ``frozendict`` / ``tuple`` rather than
            ``dict`` / ``list``.
    """

    collection_id: str
    query: str
    columns: tuple[str, ...]
    rows: FrozenFxtrCbor


@dataclasses.dataclass(frozen=True)
class DqlSchema(DataclassEntity, fxtr_type=DQL_SCHEMA_TYPE_ID):
    """Live schema for a collection — what tables / columns DQL can
    see, with backing types.

    The structure under :attr:`schema` is whatever the Docent API
    returns verbatim (frozen for storage), so unpack defensively:
    fields like ``"tables"`` may be present with descriptions of
    each table's columns, and the exact key names can evolve. For
    a stable reference to the five tables this proxy currently
    exposes, see the docstring of :class:`DocentProto`.

    Attributes:
        collection_id: The collection the schema was fetched for.
        schema: Frozen mapping of the raw schema response.
    """

    collection_id: str
    schema: FrozenFxtrCbor


@dataclasses.dataclass(frozen=True)
class MetadataFields(DataclassEntity, fxtr_type=METADATA_FIELDS_TYPE_ID):
    """Listing of metadata fields actually present on agent runs in
    a collection.

    Each entry in :attr:`fields` is a frozen mapping with at least
    ``"name"`` (the dotted JSON key, e.g. ``"agent.model_name"``)
    and ``"type"`` (a string label like ``"string"``, ``"number"``,
    ``"object"``). When :attr:`include_sample_values` was true at
    fetch time, entries also carry ``"sample_values"`` and
    ``"total_unique_values"`` — useful for telling apart a free-form
    string field from a low-cardinality enum. Entries may also
    carry extra annotation keys (e.g. ``"parquet_indexed"``) that
    vary across Docent server versions; treat the per-entry shape
    as best-effort.

    Attributes:
        collection_id: The collection the fields were fetched from.
        include_sample_values: Whether the fetch asked for samples.
        sample_limit: Cap on samples per field, when included.
        fields: Frozen tuple of per-field info mappings.
        total_runs: Total number of agent runs the metadata was
            aggregated over (``None`` on SDK versions that don't
            surface this in the response).
    """

    collection_id: str
    include_sample_values: bool
    sample_limit: int
    fields: FrozenFxtrCbor
    total_runs: int | None = None


@service_protocol
class DocentProto(Protocol):
    """Typed view of the demo's docent-proxy service.

    All methods are scoped to a single ``collection_id`` (a UUID
    string identifying one Docent collection — a collection holds
    the agent runs from one experiment or benchmark). Pass the same
    ``collection_id`` to every call in a session unless you really
    mean to switch collections; DQL is single-collection by design.

    DQL surface area — what tables you can ``SELECT`` from:

    - ``agent_runs`` — one row per agent run.
      Columns: ``id``, ``collection_id``, ``name``, ``description``,
      ``metadata_json``, ``created_at``.
    - ``transcripts`` — one row per transcript on an agent run.
      Columns: ``id``, ``collection_id``, ``agent_run_id``, ``name``,
      ``description``, ``transcript_group_id``, ``messages``,
      ``metadata_json``, ``created_at``.
    - ``transcript_groups`` — hierarchical groupings of transcripts.
      Columns: ``id``, ``collection_id``, ``agent_run_id``, ``name``,
      ``description``, ``parent_transcript_group_id``,
      ``metadata_json``, ``created_at``.
    - ``judge_results`` — scored rubric outputs.
      Columns: ``id``, ``agent_run_id``, ``rubric_id``,
      ``rubric_version``, ``output``, ``result_metadata``,
      ``result_type``.
    - ``results`` — individual LLM analysis outputs from result sets.
      Columns: ``id``, ``result_set_id``, ``llm_context_spec``,
      ``prompt_segments``, ``user_metadata``, ``output``,
      ``error_json``, ``input_tokens``, ``output_tokens``,
      ``model``, ``created_at``.

    Allowed DQL syntax (subset of Postgres SQL):

    - ``SELECT`` (with explicit projection — ``SELECT *`` is **not**
      allowed; ``COUNT(*)`` is **not** allowed, use
      ``COUNT(column_name)``), ``DISTINCT``, ``FROM``, ``WHERE``,
      subqueries, CTEs (``WITH``), all join flavours,
      ``UNION`` / ``INTERSECT`` / ``EXCEPT``, ``GROUP BY``,
      ``HAVING``, ``ORDER BY``, ``LIMIT`` / ``OFFSET``.
    - Aggregations: ``COUNT``, ``AVG``, ``MIN``, ``MAX``, ``SUM``,
      ``STDDEV_POP/SAMP``, ``VAR_POP/SAMP``, ``ARRAY_AGG``,
      ``STRING_AGG``, ``JSON_AGG`` / ``JSONB_AGG``,
      ``JSON_OBJECT_AGG``, ``PERCENTILE_CONT/DISC`` with
      ``WITHIN GROUP``.
    - Window functions: ``ROW_NUMBER``, ``RANK``, ``DENSE_RANK``,
      ``NTILE``, ``LAG`` / ``LEAD``, ``FIRST_VALUE`` /
      ``LAST_VALUE`` / ``NTH_VALUE``, ``PERCENT_RANK``,
      ``CUME_DIST``.
    - JSON ops: ``->``, ``->>``, ``#>``, ``#>>``, ``@>``, ``?``,
      ``?|``, ``?&``, ``jsonb_build_object``,
      ``jsonb_build_array``, ``jsonb_set``, ``jsonb_path_query``,
      ``jsonb_path_exists``.
    - Strings: ``SUBSTRING``, ``LEFT`` / ``RIGHT``, ``LENGTH``,
      ``UPPER`` / ``LOWER`` / ``INITCAP``, ``TRIM``, ``REPLACE``,
      ``SPLIT_PART``, ``POSITION``, ``CONCAT`` / ``CONCAT_WS``.
    - Conditionals & null helpers: ``CASE``, ``COALESCE``,
      ``NULLIF``.
    - Casts: ``CAST(... AS ...)`` and ``::type`` shorthand.
    - Date/time: ``CURRENT_DATE`` / ``CURRENT_TIME`` /
      ``CURRENT_TIMESTAMP`` / ``NOW()``, ``EXTRACT``,
      ``DATE_TRUNC``, ``AGE``, ``AT TIME ZONE``, interval
      arithmetic.
    - Arrays: ``ARRAY[...]``, ``array_cat`` / ``array_length`` /
      ``cardinality`` / ``unnest`` / ``= ANY`` / ``= ALL``.

    DQL gotchas:

    - Wildcards (``*``) are rejected — list columns explicitly.
      ``COUNT(*)`` is rejected — use ``COUNT(<column>)``.
    - Query results are capped at 10,000 rows. For larger pulls,
      paginate with ``LIMIT`` / ``OFFSET``.
    - JSON metadata access: ``metadata_json->'parent'->>'child'``
      (nested), ``metadata_json->>'flat.key'`` (some collections
      flatten nested keys with dots — the schema describes which
      layout a column uses), and ``metadata_json ? 'key'`` to test
      key existence. JSON comparisons default to string semantics;
      ``CAST`` to a numeric type before aggregating.
    - ``GROUP BY`` doesn't accept select-list aliases when selecting
      directly from ``agent_runs``. Wrap the projection in a
      subquery and ``GROUP BY`` the inner column names.
    - Avoid building ``IN (...)`` clauses with f-string interpolation
      over user-supplied strings — task names containing ``::`` get
      parsed as Postgres type casts. Fetch the candidate set and
      filter in Python instead.

    JSON access cheat sheet:

    .. code-block:: sql

        -- nested field
        metadata_json->'agent'->>'model_name'

        -- key existence
        WHERE metadata_json ? 'environment'

        -- cast for numeric aggregation
        AVG(CAST(metadata_json->>'latency_ms' AS DOUBLE PRECISION))
    """

    async def execute_dql(
        self,
        collection_id: str,
        query: str,
    ) -> EntityRef[DqlResult]:
        """Execute a DQL query against ``collection_id`` and return
        an :class:`EntityRef[DqlResult]`. The returned entity carries
        the column names + rows verbatim — :func:`dql_result_to_dicts`
        is the easiest way to consume them.

        Bad queries surface as ``invoke_error`` on the agent's slot
        carrying the underlying HTTP error message.
        """
        ...

    async def get_dql_schema(self, collection_id: str) -> EntityRef[DqlSchema]:
        """Fetch the DQL schema for ``collection_id``: which tables
        and columns are visible, with their backing types. The five
        tables documented on :class:`DocentProto` are the stable
        contract; the schema response can include collection-specific
        nuances (e.g. precise types on JSON-extracted columns).
        """
        ...

    async def get_metadata_fields(
        self,
        collection_id: str,
        include_sample_values: bool = False,
        sample_limit: int = 10,
    ) -> EntityRef[MetadataFields]:
        """List the metadata fields present on agent runs in
        ``collection_id``. Pass ``include_sample_values=True`` to
        also pull a handful of example values per field — usually
        worth it the first time you're getting oriented in a
        collection, since the JSON shape isn't otherwise visible
        from the schema call alone.
        """
        ...


def dql_result_to_dicts(result: DqlResult) -> list[dict[str, Any]]:
    """Convert :class:`DqlResult.rows` (tuple-of-tuples) into a list
    of ``column -> value`` dicts, in row order. Identity helper —
    pure Python, no SDK calls.
    """
    # ``rows`` is typed as ``FrozenFxtrCbor`` (a broad union) but the
    # host always builds it as a tuple of row-tuples — see
    # :class:`docent_service.DocentImpl.execute_dql`.
    rows = cast(tuple[tuple[Any, ...], ...], result.rows)
    return [dict(zip(result.columns, row)) for row in rows]


async def schedule_execute_dql(
    slot_key: str,
    *,
    collection_id: str,
    query: str,
) -> None:
    """Schedule a DQL execute. Result lands on ``slot_key`` as an
    :class:`EntityRef[DqlResult]`.

    Read the outcome from a subsequent ``run_script`` via
    ``retrieve_result(slot_key, EntityRef[DqlResult])`` and
    ``.load()``, or pass ``wait_seconds=N`` to the enclosing
    ``run_script`` to block inline.

    On HTTP / query failure the slot promotes to ``invoke_error``
    carrying the upstream error message.
    """
    from _fxtr_mcp_builtin import lookup_service_by_name, schedule_invoke

    ref = await lookup_service_by_name(DOCENT_PROXY_EXTERNAL_NAME)
    typed = ref.replace_service_type(DocentProto)
    await schedule_invoke(
        slot_key,
        typed,
        lambda d: d.execute_dql(
            collection_id=collection_id,
            query=query,
        ),
    )


async def schedule_get_dql_schema(
    slot_key: str,
    *,
    collection_id: str,
) -> None:
    """Schedule a DQL schema fetch. Result lands on ``slot_key`` as
    an :class:`EntityRef[DqlSchema]`. Same wait-semantics as
    :func:`schedule_execute_dql`.
    """
    from _fxtr_mcp_builtin import lookup_service_by_name, schedule_invoke

    ref = await lookup_service_by_name(DOCENT_PROXY_EXTERNAL_NAME)
    typed = ref.replace_service_type(DocentProto)
    await schedule_invoke(
        slot_key,
        typed,
        lambda d: d.get_dql_schema(collection_id=collection_id),
    )


async def schedule_get_metadata_fields(
    slot_key: str,
    *,
    collection_id: str,
    include_sample_values: bool = False,
    sample_limit: int = 10,
) -> None:
    """Schedule a metadata-fields fetch. Result lands on ``slot_key``
    as an :class:`EntityRef[MetadataFields]`. Same wait-semantics as
    :func:`schedule_execute_dql`.

    With ``include_sample_values=True`` each returned field also
    carries up to ``sample_limit`` example values plus a count of
    distinct values seen — handy for telling whether a string field
    is a tight enum or open-ended text.
    """
    from _fxtr_mcp_builtin import lookup_service_by_name, schedule_invoke

    ref = await lookup_service_by_name(DOCENT_PROXY_EXTERNAL_NAME)
    typed = ref.replace_service_type(DocentProto)
    await schedule_invoke(
        slot_key,
        typed,
        lambda d: d.get_metadata_fields(
            collection_id=collection_id,
            include_sample_values=include_sample_values,
            sample_limit=sample_limit,
        ),
    )


__all__ = [
    "DOCENT_PROXY_EXTERNAL_NAME",
    "DQL_RESULT_TYPE_ID",
    "DQL_SCHEMA_TYPE_ID",
    "METADATA_FIELDS_TYPE_ID",
    "DocentProto",
    "DqlResult",
    "DqlSchema",
    "MetadataFields",
    "dql_result_to_dicts",
    "schedule_execute_dql",
    "schedule_get_dql_schema",
    "schedule_get_metadata_fields",
]
