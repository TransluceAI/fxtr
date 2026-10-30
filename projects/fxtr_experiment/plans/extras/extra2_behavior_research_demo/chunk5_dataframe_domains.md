# Chunk 5 — dataframe domains + gather in the analysis

**Status: implemented** (2026-07-18). The deferred followup "Dataframe-native domains +
`gather` in the analysis" (overview.md), now that phase 5 (dataframes)
landed. One commit. Doubles as the live material for verifying the
dashboard's frame rendering (the renderer-consolidation browser pass
had no dataframe cells to look at).

## The changes

1. **Domain cells become zero-axis dataframe cells** (the design docs'
   canonical domain-cell shape — the schema *is* the semantic axis):

   | path | schema |
   |---|---|
   | `domain/active_subject_models` | `(subj_id: str)` |
   | `domain/active_user_sims` | `(sim_id: str)` |
   | `domain/active_judges` | `(judge_id: str)` |

   `domain/sweep_config` stays an entity cell (a scalar config, not a
   list). The `ActiveIds` entity type is deleted everywhere (entities +
   `__init__`, the flake's `behaviorTypes.activeIds`, the
   `behavior_entities` renderer's `active_ids` subtype, tests).
   Column names match the corresponding axis names on purpose — the
   driving-table `on=` map reads as identity.

2. **`evidence/judgements` grows a frame output**:
   `outputs={"verdict": EntityOutput(), "data":
   FrameOutput(schema=(score: float))}` — one row per cell, the
   analysis-relevant scalar of each judgement. `judge_task` returns the
   dict form `{"verdict": <id>, "data": <1-row frame>}`
   (`task_service.normalize_outputs`: bare returns are a single-output
   convenience only). `JUDGE_DATA_SCHEMA` lives in `judge_task.py`
   (host-side — the schema type comes from `fxtr_experiment.model`,
   which must NOT enter `behavior_research_types`: the marker package
   feeds the type-entities venv, which deliberately doesn't depend on
   `fxtr_experiment`).
   **Ripple**: `improve_sim_task`'s internal `judgements` grid runs the
   same fn, so it declares the same outputs and its read becomes
   `.output_entity("verdict")`; it simply gains per-iteration score
   frames.

3. **`analyses/sales_report` gathers and grows a frame output**:
   `outputs={"report": EntityOutput(), "evidence":
   FrameOutput(schema=(subj_id: str, sim_id: str, replica: int,
   judge_id: str, score: float))}`. New rule input
   `judgement_data = judgements.output_frame("data")` (all four axes
   kept) beside the entity view (now `.output_entity("verdict")`).
   `analyze_task`:
   - reads the three domain frames (`read_table`) + the sweep entity,
   - builds the driving key table as the polars cross join
     (subjects × sims × replicas × judges; `replica` int),
   - `gather(driving, on={axis: same-named column, ×4})` — task-side
     gather demands flow through self-vouching requests (no per-cell
     targets; proven by the MH-aggregate e2e test), pulling the whole
     evidence pipeline exactly as the point reads did,
   - aggregates the gathered table with polars (mean/count grouped by
     subj × judge, sorted) for the entity's rows,
   - still fans out the concurrent point reads of the entity outputs —
     now only for the `row_sources` verdict-link provenance,
   - returns `{"report": <AnalysisTable id>, "evidence": <the gathered
     table>}` — the frame output is the raw per-judgement evidence
     table (key columns + score; the thing you'd hand to duckdb), the
     aggregate lives in the entity as before.

4. **Driver** (`behavior_demo_helper_service.py`): `open_dataframe`
   for the three domain cells; `stage_inputs` writes id frames via
   `write_table(make_frame(...))` (same idempotent-vs-conflict
   semantics as `_write_value`); `stage_augment` overwrites via
   `wf.overwrite_table`; `_domains` reads contents via inspect (still
   never demands) + `read_table` once populated; `run_analysis`
   demands `.output_entity("report")`.

Grid identity changes (rules + outputs) — fine for the demo: state is
scaffold-wiped, and the driver's create-or-attach conflicts only
against pre-change state dirs.

## Tests

- `test_behavior_types`: drop the `ActiveIds` cases.
- `test_behavior_driver`: mocked e2e additionally asserts the analysis
  `evidence` frame (`demand_table` on `.output_frame("evidence")`:
  row count = the domain cross product, scores match the entity's
  aggregation) and a judgement `data` frame; structural test keeps its
  shape (status output unchanged).

## Docs on landing

- overview.md: the grid-namespace table rows (domain cells → dataframe
  kind with schemas; judgements/analysis outputs), the task-wiring
  section, the "Domain cells are entity-valued id lists" paragraph
  (now current-state: dataframe cells + gather), the deferred-followup
  strike, the chunks list (this file).
- The demo README's behavior-scenario blurb if it mentions domain
  cells.
- Update CODE_OVERVIEW.md (no structural change expected — the demo
  is its own tree; check `examples/fxtr_experiment_demo/README.md`).

## Implementation steps

- [x] `judge_task.py`: `JUDGE_DATA_SCHEMA` + dict return;
      `improve_sim_task.py`: internal grid outputs + named entity read.
- [x] `analyze_task.py`: domain-frame reads, driving table, gather,
      aggregation over the gathered scores, dict return
      (`EVIDENCE_SCHEMA` here).
- [x] Driver: `open_dataframe` domains, frame write helpers, wiring
      (`outputs=` on judgements + analysis, `judgement_data` input),
      `_domains` via frames, `run_analysis` named output. Also the
      `inspect` per-output key rename (`results["verdict"]`) in
      `improve_sim`.
- [x] Delete `ActiveIds` (types package, flake, renderer, exports).
- [x] Tests updated + green (`uv run pytest` in the demo).
- [x] Docs (overview.md et al) + check off here.

## Implementation notes

- **Canonical frame row order is not value order**: rows sort lex on
  their canonical *encoded bytes*, and CBOR text strings carry a
  length prefix — so `read_table` returns e.g. `pushy` before
  `baseline`. Consumers wanting value-sorted id lists sort after
  reading (the driver's `_load_domain_ids`, the analysis's row
  iteration).
- The analysis's `evidence` frame output is the *gathered* table
  (driving key columns + `score`) — gather's output shape matches the
  declared schema by name set + exact dtypes with no reordering
  needed, since the driving columns come from canonical frame reads
  (str) and a range-built int64 `replica` column.
