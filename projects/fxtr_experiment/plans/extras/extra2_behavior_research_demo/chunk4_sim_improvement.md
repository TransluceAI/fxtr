# Chunk 4 — the user-sim improvement transformation

**Status: DONE.** Implemented as written, with these notes:

- `SimCandidate` is a full `DataclassEntity` held via inline
  ``Child[SimCandidate]`` fields on the result (a plain nested
  dataclass can't carry `EntityRef` fields — mashumaro's ref strategy
  lives on the entity base), which also gives candidates their own
  renderer/panel.
- The candidate's trial transcript ref is taken from the nested
  judgement's own provenance ref (the in-data refs composing).
- The improvement e2e asserts the trial transcripts' `sim_config`
  refs equal the candidate bios — proof the nested rollouts really
  ran the rewritten personas.
- Background demands generalized to a labeled `_spawn_demand` (state
  in `status()`'s `background` map, tracked cancel scopes for
  `crash_runner`).

A super-simplified version of ABR's agentic
content-rewriting: a standalone, state-isolated transformation that
uses an LLM to improve a user-sim persona, testing each candidate by
**running the same rollout/judge task logic in a new place** (its own
internals) — the ABR "component run" shape on fxtr_experiment.

## The shape

Driver-side (all user-sourced), parameterized by
`(sim_id, subj_id, judge_id, instruction)` — **required args**, entered
in the driver panel's new input row:

1. **Select evidence** via labeled inspection: for the fixed
   `(subj_id, sim_id, judge_id)`, loop replicas from the current sweep
   config and include every pair where both the rollout and the
   judgement are *complete* — **any currency** (the refs pin exact
   versions; a stale-but-real data point is an honest input).
2. **Build the request**: a `UserSimImprovementRequest` entity —
   the instruction, the improver provider/model, and EntityRefs to the
   sim/subject/judge/rollout configs plus the selected transcripts and
   judgements (parallel tuples). The request is a **pinned snapshot**:
   configs travel inside it, deliberately detached from the live
   `configs/` grids (editing the original sim later does NOT stale a
   past transformation — the ABR immutable-ref keying, mirrored).
3. **Create the transformation cell**: `transformations/` subtree →
   `user_sim_improve` subtree grid (axis `request_id: str`, coordinate
   `f"{sim_id}-{timestamp}"` — minted driver-side where nondeterminism
   is legal; re-pressing mints a new request, never collides) → two
   child grids (one config per key; every request cell is identical):
   - `request` — scalar data grid holding the request entity.
   - `flow` — scalar task grid running `tasks/improve_sim`, wired:
     the `request` grid (subtree axis vmap-consumed), the three LLM
     service handle grids, and the `tasks/rollout` + `tasks/judge`
     handle grids (granted onward so the flow can wire its internal
     task grids to the *same* task fns — holding is not observing).
4. **Target + demand `flow`** in the background (the run_analysis
   pattern).

## The flow task fn (`improve_sim_task`)

Outer level (deterministic; every LLM call inside a checkpointed
step; `ctx.log` progress throughout):

- Load the request + its refs; `read_key` the improver from
  `subject_services[provider=request.improver_provider]`.
- Create internals up front: scalar data grids `subject_cfg` /
  `rollout_cfg` / `judge_cfg` (written from the request's refs — the
  entities are in the request's closure), `bios[iteration:int]`, and
  internal task grids `rollouts[iteration]` + `judgements[iteration]`
  wired exactly like the top-level evidence grids (vmapped `bios`,
  `int_coord("iteration")` as the rollout's `replica`, handle views
  forwarded) — same task logic, new place, isolated state.
- One string-keyed checkpoint grid `steps`; the loop (2 improvement
  rounds, then the choice):
  1. step `propose_0` / `propose_1`: format the evidence as XML
     (original transcripts + judgements; from round 1 on, also each
     prior candidate's bio, notes, transcript, judgement), send with
     the instruction to the improver, **parse inside the step**
     (`<notes>…</notes>` + `<persona>…</persona>`, judge-style
     retry-up-to-3 on unparseable output — parse failures must fail
     the *step*, so nothing malformed is ever checkpointed and a
     re-run re-samples).
  2. outer: build the candidate `UserSimConfig` (original task_prompt,
     new persona), write `bios[i]`, then read
     `judgements[i]` (demanding the nested rollout + judgement —
     their generations run inside this run and resume independently
     after a crash).
  3. step `final_choice`: all candidates + their evidence as XML; the
     model picks the final persona (repeats allowed) + notes; parse
     in-step with retries.
- Output: a `UserSimImprovementResult` — the final bio +
  final notes, EntityRef to the request, and per-iteration
  `SimCandidate` records (bio ref, the model's notes, transcript ref,
  judgement ref) — the whole improvement trajectory as in-data
  provenance.

Formatting (entity → XML) is pure outer-level code in the task module;
steps receive pre-formatted strings + the handle key and own
call+parse+retry.

## Mock mode

`MockSubjectModel.reply` sniffs the improver prompts (the system
prompt literally contains the `<persona>` format instruction) and
returns a deterministic, visibly fake, correctly-tagged
`<notes>…</notes><persona>…</persona>` reply (hash-varied like the
other mock output) — no lenient parsing anywhere; the mock speaks the
contract.

## Driver + panel

- Proto: `improve_sim(sim_id, subj_id, judge_id, instruction)` (all
  required), plus `crash_runner` / `revive_runner` (the pipeline
  driver's arc: cancel tracked background-demand cancel scopes first,
  then supervisor.crash(); revive re-registers). Background demands
  (run_analysis + improve flows) get tracked cancel scopes.
- Panel: the stage groups gain a **Transform** group — four text
  inputs (persisted in panel state) + "Improve user sim" firing the
  invoke with the four args — and Run controls gain crash/revive
  buttons (durable-resumption testing: crash mid-flow, revive,
  re-demand; completed steps + completed nested generations replay
  from durable state).

## Renderers + wiring

- `behavior_entities` gains `user_sim_improvement_request` (instruction,
  config refs, source evidence ref lists) and
  `user_sim_improvement_result` (final persona + notes, candidate
  cards with notes + bio/transcript/judgement RefLinks) subtypes;
  flake `behaviorTypes` gains the two id-strings.
- `keys.py` gains `task: improve_sim`; the runner mount binds the new
  task fn in every mode.

## Tests

- Mock e2e: stages 1–2, demand the needed judgement cells, then a
  foreground `improve_sim` demand; assert the result's shape (2
  candidates + final), the ref chain loads, the internal grids exist
  via internals browsing, and the flow cell is complete/fresh.
- Crash/revive stays manual (the in-process crash teardown hang is the
  known fxtr2 followup that keeps the pipeline demo's test xfailed).
