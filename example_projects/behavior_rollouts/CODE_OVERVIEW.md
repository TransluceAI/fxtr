# behavior_rollouts — code overview

The maintained example of a project using behaviors: scripted and provider-backed conversations
run as fxtr steps and stored as behaviors `ChatRollout` entities. An independent uv/fxtr project
with editable fxtr and behaviors dependencies, its own lockfile, `[tool.fxtr]` module registration,
a `fxtr.local.toml.example`, and a project view bundle. `README.md` has the experiments, setup, and
launch options; `../README.md` has what the example projects share.

```
behavior_rollouts/
├── pyproject.toml          the project, [tool.fxtr] modules, [tool.fxtr.views], pytest and
│                           Pyright config
├── fxtr.local.toml.example what the gitignored local configuration looks like
├── launch_scripted.py, launch_openai.py  launchers using fxtr.project.running's shared execution
│                           helpers; jobs persist in the project's own Postgres schema
├── src/behavior_rollouts/
│   ├── scripted_rollout.py EchoAPI, the rollout step, and the mapped workflow
│   └── openai_rollout.py   question sequences, validation, the tagged metadata records,
│                           provider rollout, the workflow, and result reporting
├── tests/                  validation and Postgres workflow tests with a fake model API
│   └── conftest.py         the shared fixtures of ../example_project_testing.py
└── views/                  behavior-rollouts-views, the project's renderer package
    ├── pnpm-workspace.yaml, pnpm-lock.yaml  the renderer's workspace boundary and dependencies
    ├── src/
    │   ├── index.ts        the experiment's invocation registrations: the OpenAI conversation and
    │   │                   comparison invocations
    │   ├── views.ts        the step and workflow names, and the declaration of the conversation
    │   │                   panel a row among several conversations links to
    │   ├── invocations.tsx, comparison.tsx  the conversation views (one in full; several as a
    │   │                   row each linking to that one alone) and the model/sequence selection
    │   │                   controls; the workflow's compare panel aligns two models' replies by
    │   │                   turn
    │   ├── rollouts.module.css  the package's styles
    │   └── bundle.ts       the bundle's entry, adding the shared behaviors entity views
    │                       (fxtr-view-behaviors) to the project's registrations
    └── test/               comparison and pending/failure rendering, and building and loading the
                            bundle
```

`example_projects/example_project_testing.py`, beside this project, supplies the Postgres test
fixtures: a throwaway schema, and the project's declared modules loaded through the real client,
using fxtr's test `FakeEnvironment` to avoid pinning the working checkout. Workflow tests need
`FXTR_TEST_DATABASE_URL`; pure data and scoring tests run without a database, and provider calls
require the explicit `llm_integration` marker. The root pytest suite collects these tests, and they
also run and typecheck in the project's own environment, with lock and Pyright pre-commit hooks.
