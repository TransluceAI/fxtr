# fxtr_experiment user guides

Entry-point documentation for people (and agents) who want to **use**
fxtr_experiment to build and run experiments — as opposed to the design
docs (`notes/design_overview.md` and friends), which are written for
people contributing to the system itself. These guides are
tutorial-style: they introduce concepts in the order you need them, walk
through worked examples, and defer to the canonical design docs for the
full precision.

**GUIDE WRITER SIDENOTE**: This whole directory is a first draft written
from the design notes and the demo code. Markers used throughout:

- **NEEDS CONFIRMATION** — a claim the guide author believes is true but
  wants checked by a system author.
- **NEEDS CODE REFERENCE** — a spot that should point at a specific
  module/function once verified.
- **TODO** — content that is deliberately incomplete (usually because
  the underlying feature is in progress).
- **OPEN QUESTION** — a genuine design/UX question surfaced by trying to
  explain the system to a newcomer.

## Reading order

1. [What is this system?](01_what_is_fxtr_experiment.md) — what
   fxtr and fxtr_experiment are for, and what kinds of experiments they
   are meant to express.
2. [Entities and scopes](02_entities_and_scopes.md) — the data layer:
   defining entity types, storing/loading them, and the scope rules you
   need to not fight the framework.
3. [Services: task fns, steps, and bindings](03_services_tasks_steps_bindings.md)
   — the code layer: what a service is, the three service-shaped things
   an experiment uses, and how to attach them to a running workflow.
4. [Building your first experiment](04_building_an_experiment.md) — the
   main tutorial: a multi-step (but statically-shaped) experiment,
   modeled on the behavior-research demo.
5. [The concept model in more depth](05_concept_model.md) — grids,
   vmapping, freshness/staleness, observation tracing, the execution
   semantics of tasks vs steps, and a brief note on grounding.
6. [Hierarchical workflows](06_hierarchical_workflows.md) — tasks that
   create more tasks: private trial pipelines, and "macro expansion"
   setup tasks that build your whole experiment tree.
7. [Collaborating via git](07_git_export_collaboration.md) — the
   export/import flow, git as the version-control layer, and the
   two-repo project shape.
8. [Rendering your data in the dashboard](08_custom_renderers.md) — how
   to write custom entity renderers so your experiment's results are
   inspectable in the workstation UI.
9. [Sandboxed task services (WIP)](09_sandboxed_task_services.md) — how
   to package a task fn or helper service so it runs in a sandbox, and
   how to bind it durably into a workflow.

If you only read two things, read guides 4 and 5.

## Where these guides sit relative to the other docs

- `README.md` (repo root) — the fxtr elevator pitch and goals.
- `v2/CODE_OVERVIEW.md`, `v2/notes/representing_data_as_entities.md`,
  `v2/notes/scope_service_lifetimes.md` — fxtr2 (the core data/service
  layer) reference docs. Guides 2–3 summarize the parts users need.
- `projects/fxtr_experiment/notes/design_overview.md` — the root source
  of truth for the workflow system's design; `view_spec.py`'s docstring,
  `notes/execution_semantics.md`, `notes/grounding.md`,
  `notes/dataframes.md`, `notes/at_rest_format.md`,
  `notes/sandboxed_bindings.md` are the canonical writeups these guides
  compress.
- `examples/fxtr_experiment_demo/` — the live worked example these
  guides constantly point into; its `behavior/` scenario is the model
  for guide 4.
