# Chunk 1 — types, LLM services, task fns

**Status: DONE.** The scenario's vocabulary and code, with no
runner/driver wiring yet (chunk 2). See `overview.md` for the grid
namespace and stage map. All sections below implemented as written,
with these notes:

- The user-sim impl always prepends a fixed kickoff user turn to the
  role-flipped conversation (the flipped list would otherwise start
  with an assistant turn, which the Messages API rejects; it doubles
  as the opening cue).
- `ActiveIds` grew a canonicalizing `make()` (sorted + deduped ids —
  the entity is fingerprinted).
- `AnalysisTable` cells are `str | int | float` (the `n` column is a
  count).
- Judge-verdict parsing tolerates fences/prose by taking the outermost
  `{...}` span; scores clamp to [0, 1]; anything else raises
  (→ errored generation).

## 1. The marker package: `behavior_research_types`

New workdir package
`examples/fxtr_experiment_demo/agent_workdir/packages/behavior_research_types/`
(the `experiment_notes` pattern: marker-style pyproject with no deps,
`src/` layout, committed `uv.lock`; the embedding venv supplies fxtr2).
Contents split across small modules (`entities.py`, `protocols.py`,
`keys.py`, re-exported from `__init__.py`), with a stable namespace
UUID for all type ids.

### Entities (mashumaro `DataclassEntity`, frozen)

- `ChatMessage(role: str, text: str)` — plain nested dataclass
  (`role` ∈ `user` / `assistant`).
- `Transcript(messages: tuple[ChatMessage, ...])` — the rollout output.
- `SubjectModelConfig(provider: str, model: str, system_prompt: str)`
- `UserSimConfig(task_prompt: str, persona: str)` — the sim's model is
  fixed by the service binding, deliberately not config (not a sweep
  axis in this demo).
- `JudgeConfig(provider: str, model: str, rubric: str)`
- `RolloutConfig(max_turns: int)` — turns = user/assistant exchanges.
- `SweepConfig(replicas: int)`
- `ActiveIds(ids: tuple[str, ...])` — the domain-cell value (sorted at
  construction for stable fingerprints).
- `JudgeResult(score: float, reasoning: str)` — score in [0, 1].
- `AnalysisTable(columns: tuple[str, ...], rows: tuple[tuple[str | float, ...], ...])`
  — the JSON-shaped result table (generic on purpose: one table
  renderer covers future analyses).
- `ServiceKey(label: str, detail: str)` — the content-addressed
  handle-key entity type (readable in handle cells, distinct from
  ExpNote).

### Service protocols (`@service_protocol`)

Three deliberately distinct protocols (identity = the contract), plain
JSON-ish wire args (messages cross as `[{role, text}, ...]` dicts;
exact wire shapes settled at implementation):

- `SubjectModelProto.reply(system_prompt, model, messages) -> str` —
  the assistant's next message given the conversation so far.
- `UserSimProto.next_message(task_prompt, persona, messages) -> str` —
  the simulated user's next message (impl flips roles internally so its
  LLM speaks as the user; the user always speaks first, so the first
  call sees an empty message list).
- `JudgeModelProto.judge(rubric, model, messages) -> {score, reasoning}`
  — scores the finished transcript against the rubric.

### Key helper (`keys.py`)

`service_key(name) -> ServiceKey` — content-addressed key entities for
every task fn and LLM service (the `fn_key_note` pattern: stable ids
across relaunches make the runner mount's auto-bind line up with handle
cells written in earlier sessions). One naming table: the three task
keys (`task:rollout`, `task:judge`, `task:analyze`) plus **per-mode**
LLM service keys (`svc:subject:anthropic` / `svc:subject:mock`,
`svc:user_sim` / `svc:user_sim:mock`, `svc:judge:anthropic` /
`svc:judge:mock`) — distinct keys per mode so a mode switch is an
observable handle-cell overwrite (see `overview.md`'s design
decisions).

## 2. Host-side LLM service impls

New directory `examples/fxtr_experiment_demo/behavior/` (added to
`sys.path` by the orchestrator/tests like the packages are), one file
per service:

- `anthropic_client.py` — the one shared seam to the API: a thin
  helper owning an `AsyncAnthropic` client (constructed by the runner
  mount in chunk 2 and passed in) with a single
  `complete(model, system, messages) -> str` coroutine; retries/timeouts
  kept minimal. `anthropic` joins the demo pyproject's deps.
- `subject_model_service.py` — `SubjectModelImpl`
  (`TypedServiceImpl[SubjectModelProto]`): passes messages through
  as-is.
- `user_sim_service.py` — `UserSimImpl`: system prompt composed from
  `task_prompt` + `persona` + fixed simulator framing; flips
  user/assistant roles so the LLM writes the user's side; fixed small
  default model (a constructor arg).
- `judge_service.py` — `JudgeImpl`: prompts for a JSON verdict
  (`score`, `reasoning`), parses defensively (clamps to [0, 1];
  a malformed verdict raises — an errored generation is the correct
  surface, retry-able from the UI).

## 3. Task fns

One file per task fn under `behavior/`, each decorated (`@task_fn`;
steps `@step_fn(type_id=...)` with a stable namespace UUID). Inside
task fns the active scope is the run scope (sanctioned user-code
style — bare `.store()` / `.load()` are correct here, as in
`demo_task_fns.py`).

- `rollout_task.py` — `rollout(ctx, *, subject, sim, rollout_cfg,
  replica, subject_services, sim_service)`:
  - outer level: read + load the two configs and `RolloutConfig`;
    `subject_services.read_key(provider=cfg.provider)` (data-dependent,
    allowed) + `sim_service.read_key()`; `replica.value()` (coord view,
    unrecorded-pure) — logged, and available to prompts if we ever want
    per-replica variation;
  - checkpoint grid `turns`, axes `(i: int)`; per exchange two steps:
    `sim_turn_step` (connect the sim key → `next_message`) then
    `subject_turn_step` (connect the subject key → `reply`), each
    taking plain-data args (prompts, model, key, messages-so-far) and
    returning the new message text — every LLM call durable;
  - `ctx.log()` per turn (streams into the cell drawer's run log);
  - output: `Transcript` entity id.
- `judge_task.py` — `judge(ctx, *, transcript, judge_cfg,
  judge_services)`: read the transcript id + load it, load the config,
  read the provider's judge key; one checkpointed step `judge_call`
  returning `{score, reasoning}`; store a `JudgeResult`, return its id.
- `analyze_task.py` — `analyze(ctx, *, judgements, active_subjects,
  active_sims, active_judges, sweep)`: read + load the four domain
  entities; loop the cross product (subjects × sims × replicas ×
  judges) reading `judgements` cells (each read is a recorded demand);
  load each `JudgeResult`; mean per (subject, judge); store + return an
  `AnalysisTable` with columns
  `("subject_model", "judge", "mean_score", "n")`. No steps —
  deterministic aggregation over recorded reads.

## 4. Tests

In `examples/fxtr_experiment_demo/tests/`:

- entity round-trips for the new types (store/load, including the
  union-celled `AnalysisTable` and nested `Transcript`);
- judge-verdict parsing unit tests (pure helper, no API);
- no LLM-service or task-fn execution tests here — execution is
  covered in chunk 2 (the structural test, the mocked end-to-end over
  the fake services, and the env-gated live smoke).

## 5. Wrap-up

- Update `examples/fxtr_experiment_demo/README.md` (new layout entries)
  and `projects/fxtr_experiment/CODE_OVERVIEW.md` if its demo-harness
  description needs the second scenario mentioned.
