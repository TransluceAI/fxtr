# basic_project — code overview

The example project: a small fxtr project set up the way a user's is, and the home of fxtr's
example experiments. It is an independent uv project (excluded from the workspace, fxtr as an
editable path dependency, its own venv and lock, a `.python-version` pin) whose package
`basic_project` holds the experiments, with launcher scripts at its root and a renderer package in
`views/`. `README.md` has the setup and the commands; `../README.md` has what the example projects
share.

```
basic_project/
├── pyproject.toml          the project, [tool.fxtr] modules (what a resume or the viewer
│                           imports), and [tool.fxtr.views] (its renderer package)
├── fxtr.local.toml.example what the gitignored local configuration looks like
├── launch_*.py             workflow launchers: running.launch_cli for Postgres jobs, with explicit
│                           concurrency and input flags for the slow and huge-grid experiments
├── src/basic_project/
│   ├── rollout_experiment.py  a small rollout-and-grade experiment: entity types, typed steps,
│   │                       replicas, and a workflow over entity arrays
│   ├── bio_refinement.py   iterative bio refinement: a child workflow with data-dependent control
│   │                       flow, mapped over an array of bios
│   ├── array_queries.py    a model evaluation analyzed with array queries, on handles and in a step
│   ├── model_harness_sweep.py  full and filtered model/harness/task sweeps with ragged
│   │                       model-specific context, nested workflows, and mock transcripts
│   ├── project_arrays.py   the project arrays walkthrough over the ProjectClient: declaring,
│   │                       filling, loading, snapshotting, and passing a snapshot to a job
│   ├── slow_mapped.py      staggered sleeps and logs across two lanes of mapped prepare/score
│   │                       steps, for watching and paging through live execution
│   └── huge_grid.py        a 5,000-cell model × prompt style × task × seed mapped step with
│                           sleeps, then per-model/style summaries of whole [t, s] slices
└── views/                  basic-project-views, the project's renderer package, built into its
    │                       view bundle
    ├── pnpm-workspace.yaml, pnpm-lock.yaml  the renderer's workspace boundary and dependencies
    ├── src/
    │   ├── types.ts        the type name constants and one TypeScript interface per type
    │   │                   mirroring its serialized form, with EntityID<...> in reference
    │   │                   positions (TranscriptData.model is an EntityID<ModelConfigData>)
    │   ├── invocation_names.ts  the registered step / workflow names of the examples, as constants
    │   ├── rollout.tsx     per type: a panel, an inline link (the name / title /
    │   │                   `transcript · N turns`), and a EntityHoverPreview; ModelName loads a
    │   │                   model's name through the typed id a transcript carries, with no cast
    │   ├── refinement.tsx  the same for the bio-refinement types
    │   ├── views.ts        the declarations of the step panels rows link to (ROLLOUTS, GRADES)
    │   ├── invocations.tsx invocation panels: RolloutPanel for the `rollout` step (one rollout
    │   │                   in full: who talked to what, the turns, the arrays; several as a
    │   │                   DataTable row per spawn, a row linking to that rollout alone);
    │   │                   GradePanel for the `grade` step (one grade's score and rationale;
    │   │                   several as a row per spawn with its score, linking the same way, and
    │   │                   their mean); and RefineBioRoundsPanel for the `refine_bio` workflow:
    │   │                   one Round per simulate/rewrite pair among the record's children, each
    │   │                   spawn's state read through useInvocation on its ref, several
    │   │                   refinements through eachInvocation
    │   ├── index.ts        the components, plus demoRenderers: one renderer list, the entity views
    │   │                   by type then the invocation panels by step / workflow name
    │   ├── views.module.css  the package's own styles (beyond the kit's layout)
    │   └── bundle.ts       the bundle's entry: the package's ViewBundle
    ├── test/               the renderers mounted on a table shell, and bundle.test.ts, which builds
    │                       through its build script with native TypeScript loading disabled where available,
    │                       then loads the bundle in Node
    └── vite.config.ts      defineViewBundleConfig({ entry: "src/bundle.ts" })
```

## The experiments

The launchers call `fxtr.project.running.launch_cli`, running a Postgres job through the shared
local-client opener and `run_job`: the client pins the code, checks the venv, creates the job, and
runs it here; the helper opens the job in the project's running viewer (or says how to start one)
immediately, then prints the status and result. The slow and huge-grid launchers parse their own
flags and pass inputs, concurrency, and environment options to `launch_cli`. Since the project lives inside the fxtr repository, a launch here pins
fxtr's own commit with `example_projects/basic_project` as the project's path.

`rollout_experiment` is an end-to-end experiment shaped like a real one: `ModelConfig`,
`Scenario`, `Rubric`, and `Transcript` entity types, one-dim arrays of references to them, a
`rollout` step mapped over models and scenarios with a `sample` replica dim, and a `grade` step
mapped over the transcripts and rubrics into an array of `Grade` TypedDicts; `rollout` asks for
its inputs as `BoundID`s and returns a `Transcript`, relying on the step decorator's
annotation-driven conversions, and `rollout_desugared` is the same step with the binds and loads
written out. `bio_refinement` builds on it: the child workflow `refine_bio` simulates a user bio
against a model, asks a rewriter step for a `Review`, and loops while there is a proposal,
observing each one and re-registering it as a source array; `refine_bios` maps the child over an
array of bios.

`array_queries` analyzes a model evaluation (a `solve` step mapped over models and problems) with
array queries, asking each question where a query can run: the query builder and `fxtr.sql` over
the workflow's handles (each chain one query node, and one observed with `item()`), in plain
functions of the handles (`triage` returns its query uncollected; `cheapest_solver` runs a window
function over two handles), and over the concrete arrays the `report` step receives, which it
queries locally. The experiments declare their array types on the bodies as Python types (see
`fxtr/src/fxtr/experiment/annotations.py`).

`model_harness_sweep` compares a full 2-model × 2-harness × 3-task grid with a sparse selection of
cross-provider pairs: each child workflow maps `run_task` over `m`, `h`, and `t`, while the
model-specific `stuff` array has dims `[s, m]`, so each call receives its model's entire ragged
`[s]` slice; the root stacks the two results along `selection`.

`slow_mapped` and `huge_grid` are for watching a run in the viewer rather than for what they
compute: staggered sleeps and progress logs across two lanes of 40 mapped samples (about four
minutes), and a 5,000-cell model × prompt style × task × seed mapped step whose summaries each
receive a whole `[t, s]` slice (about eight minutes at 16 concurrent steps; much larger grids
currently stall the Postgres runner). Their launchers take options sizing the run, scaling its
sleeps, and bounding its concurrency, and pass a fresh cache namespace as a root input, so a rerun
sleeps again.

`project_arrays.walkthrough(client)` is the notebook-style walkthrough of the project arrays over
the `ProjectClient`: declare an array of note ids, store `Note` entities and upsert an array
referencing them, load it back and follow a reference, snapshot, edit, and snapshot again to see
unchanged rows give the same snapshot and identical content one entity, then pass a snapshot's
bound id to a job and see the root's input node reference that very entity.

Every step and workflow in the package declares an explicit `name=`, since the renderers in
`views/` are keyed on those names.

## The renderers

`views/` is the reference example of a project's renderer package and of the authoring
conventions in `ts/CODE_OVERVIEW.md`: it renders the entity types of `rollout_experiment` and
`bio_refinement` (ModelConfig, Scenario, Rubric, Transcript; Bio, RewriteInstructions,
SimulatedTranscript) and their steps and workflows (`rollout`, `grade`, `refine_bio`).

## Tests

The project has no tests of its own: fxtr's tests import its package (fxtr's pytest `pythonpath`
and Pyright `extraPaths`) and run the experiments and the walkthrough through the local client
over a fake environment, in `fxtr/tests/test_example_*.py`. The opt-in
`test_example_project_launch.py` (`FXTR_TEST_EXAMPLE_PROJECT=1`) copies the project into a fresh
git repository with its own venv and runs a launcher there through `uv run`, so the real
environment pins a commit and checks a venv without touching this repository. Pre-commit keeps the
project's lock current against fxtr's dependencies and typechecks it in its venv. The renderers'
tests run with `pnpm --dir views test`.
