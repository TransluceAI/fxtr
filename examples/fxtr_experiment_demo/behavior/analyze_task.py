"""The analysis task fn: aggregate judgements into a score table.

Runs on the ``analyses/`` task cell. The outer level reads the three
``domain/`` dataframe cells (single-column id frames — the open-map
principle's domain cells in their canonical zero-axis-frame form) plus
the sweep config, builds the (subject x sim x replica x judge) driving
table as a polars cross join, and **gathers** the judgement task's
``data`` frame output across it — one effectful op whose request edges
demand the whole evidence pipeline (requests are self-vouching, so no
per-cell targets are needed; misses resolve concurrently, so the
sweep's LLM work overlaps). The verdict *entities* are then point-read
concurrently as before — solely for the per-row provenance refs the
:class:`behavior_research_types.AnalysisTable` carries.

Two named outputs: ``report`` — the ``AnalysisTable`` entity with the
mean score per (subject model, judge) over sims x replicas — and
``evidence`` — the gathered per-judgement table itself (key columns +
score; the relational form you'd hand straight to polars/duckdb).

Determinism: the gathered table arrives in driving-row x canonical
cell-row order, the driving table is built from canonically-ordered
domain frames, and the aggregation below iterates those same id lists
— completion order never enters any output.

Scope note: bare ``.store()`` / ``.load()`` — run-scope user-code
style, as in ``rollout_task.py``.
"""

from __future__ import annotations

import anyio
import polars as pl
from behavior_research_types import AnalysisTable, JudgeResult, SweepConfig
from fxtr2.data.entity_ref_scope import EntityRef
from fxtr_experiment_task import model as m
from fxtr_experiment_task.task_api import TaskContext
from fxtr_experiment_task.task_adapter import task_fn
from fxtr_experiment_task.views import DataFrameView, DataView

#: The ``evidence`` frame output's schema: the gathered judgement data
#: (the driving key columns + the judge task's ``data`` column).
EVIDENCE_SCHEMA: m.FrameSchema = (
    m.ColumnSpec("subj_id", "str"),
    m.ColumnSpec("sim_id", "str"),
    m.ColumnSpec("replica", "int"),
    m.ColumnSpec("judge_id", "str"),
    m.ColumnSpec("score", "float"),
)


@task_fn
async def analyze_task(
    ctx: TaskContext,
    /,
    *,
    judgements: DataView,
    judgement_data: DataFrameView,
    active_subjects: DataFrameView,
    active_sims: DataFrameView,
    active_judges: DataFrameView,
    sweep: DataView,
) -> dict[str, object]:
    """See the module docstring.

    Args (all views, wired by the driver's analysis rule):
        judgements: The ``evidence/judgements`` ``verdict`` output view
            with all four axes kept — point-read per coordinate for the
            provenance refs.
        judgement_data: The ``evidence/judgements`` ``data`` frame
            output view, same four axes kept — the gather target.
        active_subjects: Zero-axis dataframe view — ``(subj_id: str)``.
        active_sims: Zero-axis dataframe view — ``(sim_id: str)``.
        active_judges: Zero-axis dataframe view — ``(judge_id: str)``.
        sweep: Fully-bound data view — :class:`SweepConfig`.
    """
    subjects_df = await active_subjects.read_table()
    sims_df = await active_sims.read_table()
    judges_df = await active_judges.read_table()
    sweep_cfg = await EntityRef(await sweep.read(), SweepConfig).load()
    # Value-sorted for the output row order (canonical frame row order
    # is lex on encoded row bytes, which sorts by string length first).
    subjects: list[str] = sorted(subjects_df["subj_id"].to_list())
    sims: list[str] = sorted(sims_df["sim_id"].to_list())
    judges: list[str] = sorted(judges_df["judge_id"].to_list())

    driving = (
        subjects_df.join(sims_df, how="cross")
        .join(pl.DataFrame({"replica": list(range(sweep_cfg.replicas))}), how="cross")
        .join(judges_df, how="cross")
    )
    await ctx.log(
        f"gathering {driving.height} judgement scores: {len(subjects)} subjects x "
        f"{len(sims)} sims x {sweep_cfg.replicas} replicas x {len(judges)} judges"
    )
    evidence = await judgement_data.gather(
        driving,
        on={"subj_id": "subj_id", "sim_id": "sim_id", "replica": "replica", "judge_id": "judge_id"},
    )
    scores: dict[tuple[str, str, int, str], float] = {
        (r["subj_id"], r["sim_id"], r["replica"], r["judge_id"]): r["score"]
        for r in evidence.iter_rows(named=True)
    }

    # Point-read the verdict entities concurrently for the per-row
    # provenance refs (all fresh after the gather above; completion
    # order lands in a coordinate-keyed dict, never in the output).
    await ctx.log(f"reading {driving.height} verdict entities for row provenance")
    fetched: dict[tuple[str, str, int, str], EntityRef[JudgeResult]] = {}

    async def fetch(subj_id: str, sim_id: str, rep: int, judge_id: str) -> None:
        ref = EntityRef(
            await judgements.read(subj_id=subj_id, sim_id=sim_id, replica=rep, judge_id=judge_id),
            JudgeResult,
        )
        fetched[(subj_id, sim_id, rep, judge_id)] = ref

    async with anyio.create_task_group() as tg:
        for subj_id in subjects:
            for judge_id in judges:
                for sim_id in sims:
                    for rep in range(sweep_cfg.replicas):
                        tg.start_soon(fetch, subj_id, sim_id, rep, judge_id)

    rows: list[tuple[str | int | float, ...]] = []
    row_sources: list[tuple[EntityRef[JudgeResult], ...]] = []
    for subj_id in subjects:
        for judge_id in judges:
            coords = [
                (subj_id, sim_id, rep, judge_id)
                for sim_id in sims
                for rep in range(sweep_cfg.replicas)
            ]
            if coords:
                cell_scores = [scores[c] for c in coords]
                rows.append((subj_id, judge_id, sum(cell_scores) / len(cell_scores), len(coords)))
                row_sources.append(tuple(fetched[c] for c in coords))

    table = AnalysisTable(
        columns=("subject_model", "judge", "mean_score", "n"),
        rows=tuple(rows),
        row_sources=tuple(row_sources),
    )
    return {"report": table.store().entity_id, "evidence": evidence}
