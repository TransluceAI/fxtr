# What is this system?

## The one-paragraph version

**fxtr** is a data layer for provenance tracking and reproducible
experiments: content-addressed, immutable, typed storage for
semi-structured data, plus a service/RPC layer for the code that
produces and consumes that data. **fxtr_experiment** is a durable
workflow system built on top of it: you describe your experiment as a
namespace of **grids** (tables of typed cells), wire **task** grids to
your own Python functions, and the system executes them
demand-driven — durably, resumably, and with every input observation
recorded, so it can always tell you whether a result is still current
and how it was produced.

## The problem it is trying to solve

Analyses of language-model behavior tend to be bespoke: you rarely know
in advance exactly what you're looking for, and you often want to use
LLMs to automate parts of the analysis itself. The two common
approaches both have problems:

- **Rigid eval frameworks** give you observability and scale, but only
  for analyses that fit their shape.
- **Vibe-coded one-off scripts** answer questions fast, but the results
  are hard to verify, hard to reproduce, and hard to extend when
  conditions change.

fxtr_experiment aims at the middle: a framework opinionated about
*verifiability*, *modularity*, and *repeatability*, but **not** about
the shape of your analysis. You express your experiment as ordinary
Python task functions operating on typed data, and the framework gives
you — without extra work per experiment:

- **Durability**: kill the process mid-experiment (or crash), relaunch,
  and re-running resumes where it left off. Every expensive operation
  (each LLM call, say) can be an individually checkpointed step.
- **Staleness tracking**: change an input — a prompt, a judge rubric, a
  model handle — and the system knows exactly which downstream results
  are invalidated and which are still valid, at per-cell granularity.
- **Provenance**: every result is an immutable, content-addressed
  entity that can carry references back to the exact configs and
  intermediate artifacts it was computed from, and the system separately
  records what every task actually read.
- **Inspectability**: a live dashboard (in the fxtr workstation UI)
  shows the whole experiment as a tree of grids — statuses, freshness,
  logs, and your own custom renderers for your own data types.
- **Portability**: the whole workflow state exports to a git-friendly
  directory of text files plus an entity store, so an experiment can be
  versioned, shared, forked, and (eventually) published to the
  Behaviors Hub.

## What kinds of things can you express?

Anything whose shape is "a family of typed results indexed by a few
axes, computed by code from other such families". Concretely, the
motivating examples:

- **A sweep-and-judge evaluation** (the MH-pilot shape): roll out
  dozens of subject models against hundreds of simulated users, judge
  each transcript with many judges, aggregate into a summary matrix —
  where each rollout and each judge call is durable and resumable, and
  widening the sweep (a new subject model, a new judge) recomputes
  exactly the new cells.
- **Behavior-research pipelines**: the worked example in these guides
  (`examples/fxtr_experiment_demo`'s behavior scenario) — AI assistants
  swept over system prompts talk to persona-driven user simulators, LLM
  judges score the transcripts against rubrics, and an analysis task
  aggregates scores into a table you can explore in the browser.
- **Content-transformation loops** (the ABR user-simulator-improver
  shape): a task that takes a snapshot of evidence, proposes improved
  artifacts with an LLM, *trials each candidate by running nested
  sub-experiments inside its own private workspace*, and returns the
  winner with its full trajectory as provenance.
- **Agent-authored experiments**: because the whole construction
  surface is one programmatic capability, an agent can be asked "answer
  this question with an experiment; use fxtr" and build a workflow
  whose every step you can afterwards inspect, re-run, and verify.

## The mental model in five sentences

1. A **workflow** is a tree-shaped namespace of **grids**; each grid is
   a table whose cells are addressed by named, typed **axes** (e.g.
   `rollouts[subj_id, sim_id, replica]`) and hold one kind of thing:
   data entities, dataframes, service **handles**, **task** results, or
   **subtrees** of further grids.
2. A **task grid** carries a stored **rule** wiring a task function (a
   Python `async def` you register) to input views of other grids; every
   cell of the grid runs the same code, with inputs sliced per-cell by
   the rule ("vmapped").
3. Execution is **demand-driven and pull-based**: you declare a
   **target** cell and demand it; reading its inputs recursively demands
   whatever isn't computed yet.
4. Every effectful operation a task performs is **recorded**; the
   records make execution durable (crash → resume by replay), verify the
   task is deterministic at its outer level, and double as the
   **observations** from which freshness/staleness is derived.
5. The human (or driving agent) manipulates the same view calculus
   through a single **user capability** — from Python, over RPC, or from
   the browser dashboard, which speaks the same protocol.

The rest of the guides unpack these one by one.

## What this system is *not* (yet)

- Not a distributed scheduler: v0 is a single runner process (though
  services it calls can live anywhere, and tasks fan out concurrently).
- Not a general database: dataframe cells are deliberately scalar-only
  and gather is the one relational primitive; heavy relational work
  happens locally in polars/duckdb on materialized frames.
- Not opinionated about your domain: it has no notion of "model",
  "judge", or "transcript" — those are entity types *you* define.

**NEEDS CONFIRMATION**: this guide frames fxtr_experiment as "the"
durable workflow layer of fxtr and treats `projects/fxtr_workflow` /
v1 tasks as historical. Is that the right framing for new users?
