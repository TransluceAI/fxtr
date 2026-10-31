"""Running array query plans in DuckDB, for local queries and for a workflow's query nodes.

The plans and their expressions are plain data in :mod:`fxtr.entity_defns.query` and
:mod:`fxtr.entity_defns.query_plan`. This package turns a plan into SQL (``compiler``), runs it
over Arrow tables and checks what comes back (``executor``), and provides the lazy query that
``Array.query()`` returns and :func:`~fxtr.duckdb_engine.local_query.query_arrays`, which
computes a workflow's query nodes (``local_query``). It depends on the entity layer and on
nothing in the runner, which calls it.
"""
