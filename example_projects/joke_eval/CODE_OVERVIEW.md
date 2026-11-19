# joke_eval — code overview

The example project of the `first-experiment` skill (`.agents/skills/first-experiment/SKILL.md`):
the experiment that the skill's guide, `docs/fxtr/first-experiment.mdx`, builds, as a project.
Models write jokes, a judge model grades them with behaviors' `judge`, and a summary averages each
model's scores. An independent uv/fxtr project with editable fxtr and behaviors dependencies, its own lockfile,
`[tool.fxtr]` module registration, and a view bundle of behaviors' renderers. `README.md` has how
to run it; `../README.md` has what the example projects share.

The guide shows `src/joke_eval/experiment.py`, `launch.py` and `launch_minimal.py` in full, and
quotes parts of them. This project is the source of truth for that code: change it here, run the
tests, and copy the result into the guide. The guide is the source of truth for the narrative around it.

```
joke_eval/
├── pyproject.toml          the project, [tool.fxtr] modules, [tool.fxtr.views], pytest and
│                           Pyright config
├── fxtr.local.toml.example what the gitignored local configuration looks like
├── launch.py               the guide's full launcher: the pilot and full sizes, the models, the
│                           judge, the prompt, the rubric and output schema, and the summary it
│                           prints
├── launch_minimal.py       the guide's first launcher, which the guide also names launch.py: one
│                           model, one topic, one joke
├── src/joke_eval/
│   └── experiment.py       the guide's experiment module: `SamplingParamsEntity`, `ModelSummary`,
│                           `JokeReport`, `make_api`, the judge's template, the steps (`tell_joke`,
│                           `transcript`, `grade_joke`, `summarize`) and the workflow `evaluate`
├── tests/
│   ├── test_experiment.py  the pilot end to end on Postgres, through the guide's launcher, with
│   │                       a fake model telling jokes and judging them; the minimal launcher
│   └── conftest.py         the shared fixtures of ../example_project_testing.py
└── views/                  joke-eval-views: behaviors' renderers, as the guide registers them
    ├── src/bundle.ts       the bundle's entry
    └── test/bundle.test.ts the bundle builds, and loads behaviors' renderers
```

Every model call takes its model as two step inputs: a name with its provider
(`anthropic/MODEL`), which `make_api` turns into a model API, and a `SamplingParamsEntity`. The
jokes and the judge share one sampling entity. The judge's instructions, rubric and output schema
are stored workflow inputs, so a change to any of them is a change to the judge step's inputs.

The tests need `FXTR_TEST_DATABASE_URL`. The root pytest suite collects them, and they also run
and typecheck in the project's own environment, with lock and Pyright pre-commit hooks.
