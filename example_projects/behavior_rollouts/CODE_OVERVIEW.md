# behavior_rollouts — code overview

The maintained example of a project using behaviors: scripted and provider-backed conversations
run as fxtr steps and stored as behaviors `ChatRollout` entities, and the provider-backed ones
judged with behaviors' judge. An independent uv/fxtr project with editable fxtr and behaviors
dependencies, its own lockfile, `[tool.fxtr]` module registration, a `fxtr.local.toml.example`,
and a project view bundle. `README.md` has the experiments, setup, and launch options;
`../README.md` has what the example projects share.

The project holds three experiments. Each is named for what it shows or asks, and every file and
registered name of an experiment carries that name: `launch_<name>.py`, `<name>.py` (its workflow,
steps, report and, where it judges, rubric and output schema), `<name>.experiment` and
`<name>.*` for its registered workflow and steps, `<Name>Report` and `<Name>Verdict`, and
`test_<name>.py`.

- `echo`: two-turn conversations with a fake echo model, and each conversation's longest reply.
  No provider calls.
- `instructions_in_force`: does a model keep to the instructions still in force as a
  conversation sets, revises and withdraws them? Its data is `instructions_in_force_sequences.py`.
- `belief_reinforcement`: does supporting a distressed user strengthen their interpretation of
  events beyond the evidence? Its data is `belief_reinforcement_cases.py`, and `EXPERIMENT.md`
  has the question, conditions and judgment definitions. The report keeps each case's situation
  and evidence condition, which are withheld from every model. Its transcript step requires a
  completed rollout.

The two judged experiments share how a step gets its model (`models.py`) and the judge step
(`transcript_judging.py`).

```
behavior_rollouts/
├── EXPERIMENT.md           the belief-reinforcement question, conditions and judgment definitions
├── pyproject.toml          the project, [tool.fxtr] modules, [tool.fxtr.views], pytest and
│                           Pyright config
├── fxtr.local.toml.example what the gitignored local configuration looks like
├── launch_echo.py, launch_instructions_in_force.py, launch_belief_reinforcement.py
│                           a launcher per experiment, using fxtr.project.running's shared
│                           execution helpers; jobs persist in the project's own Postgres schema.
│                           The judged experiments' launchers store each model's sampling and the
│                           judge's instructions, rubric and output schema they pass; the
│                           belief-reinforcement one also selects cases and a cache root
├── src/behavior_rollouts/
│   ├── models.py           how a step gets the model it calls: `SamplingParamsEntity`, the
│   │                       provider prefix (`ProviderName`, `split_provider`, `make_api`), the
│   │                       spec parser, and `store_model_specs`, which stores a dimension's
│   │                       sampling and returns its model names and sampling as two arrays
│   ├── transcript_judging.py  `judge_transcript`, the judge step both judged experiments
│   │                       schedule, and what they share: the context template, `TRANSCRIPT`,
│   │                       `JUDGE_INSTRUCTIONS`, and the citation formats with the one-citation
│   │                       validator
│   ├── echo.py             EchoAPI, the rollout step, the step picking each conversation's
│   │                       longest reply (`LongestReply`, with a `MessageSelector` into the
│   │                       conversation), and the mapped workflow
│   ├── instructions_in_force.py  the rubric and output schema, the workflow and its steps (the
│   │                       conversation, its transcript, reading the verdicts), the tagged
│   │                       metadata records, `InstructionsInForceVerdict`,
│   │                       `InstructionsInForceReport`, validation, and result reporting
│   ├── instructions_in_force_sequences.py  the system prompt, and the named sequences of user
│   │                       turns, each commented with what it tests
│   ├── belief_reinforcement.py  the rubric and output schema, the workflow and its steps,
│   │                       `BeliefReinforcementVerdict`, `BeliefReinforcementReport`, and result
│   │                       reporting
│   └── belief_reinforcement_cases.py  five situations, each in three evidence conditions with
│                           four user turns, and `matched_cases`
├── tests/                  validation and Postgres workflow tests with fake model APIs
│   ├── test_echo.py        the echo workflow and its longest replies
│   ├── test_instructions_in_force.py  the workflow end to end with a fake subject and a fake
│   │                       judge that cites the transcript, and the sampling each call used;
│   │                       sequence validation
│   ├── test_belief_reinforcement.py  matched-case isolation, and the workflow's stored judgments
│   │                       and the model each call went to
│   ├── test_models.py      model specs, provider prefixes, `store_model_specs`, and the sampling
│   │                       entity's round trip
│   ├── test_transcript_judging.py  the one-citation validator
│   └── conftest.py         the shared fixtures of ../example_project_testing.py
└── views/                  behavior-rollouts-views, the project's renderer package
    ├── pnpm-workspace.yaml, pnpm-lock.yaml  the renderer's workspace boundary and dependencies
    ├── src/
    │   ├── index.ts        every registration: each experiment's entity and invocation views,
    │   │                   and the sampling entity's
    │   ├── views.ts        the registered workflow and step names the views key on, and the
    │   │                   declaration of the conversation panel a row among several
    │   │                   conversations links to
    │   ├── sampling.tsx    the `SamplingParamsEntity` link and panel, which name the settings
    │   │                   that are set
    │   ├── echo.tsx        the `LongestReply` entity view, linking to its conversation with its
    │   │                   selector, which the shared conversation view marks
    │   ├── instructions_in_force/
    │   │   ├── report.tsx  the report's wire shape, and `WithReport`, which reads an invocation's
    │   │   │               output through the report it holds
    │   │   ├── overview.tsx  the results overview, from the report's verdicts; each verdict opens
    │   │   │               its judgment in fxtr-view-behaviors' judgment view
    │   │   ├── conversations.tsx  the conversation views: one in full, or several as a row each
    │   │   │               linking to that one alone, with the model/sequence selection controls
    │   │   └── comparison.tsx  the compare panel, aligning two models' replies by turn
    │   ├── belief_reinforcement.tsx  the results overview (metric selection, disagreement
    │   │                   filtering), the conversation views, and the report entity
    │   ├── rollouts.module.css  the package's styles
    │   └── bundle.ts       the bundle's entry, adding the shared behaviors entity views
    │                       (fxtr-view-behaviors) to the project's registrations
    └── test/               the instructions-in-force report-backed views, belief-reinforcement
                            metrics and missing-answer handling, comparison and pending/failure
                            rendering, the sampling entity's views, and building and loading the
                            bundle
```

The instructions-in-force workflow runs four steps, each mapped over the dims it needs: the
conversations (`[model, sequence]`), their transcripts (the same), the judgments
(`[model, sequence, judge]`, with a `[sample]` replica dim), and one step reading every
judgment's verdict into a small array. It returns an `InstructionsInForceReport` referring to the
four arrays, which the launcher prints from and the workflow's views read
(`instructions_in_force/report.tsx`). The belief-reinforcement workflow has the same shape over
`[model, case]`. A judgment refers to its entities by ID: the transcript is a stored step output,
and the judge's instructions, rubric and output schema are workflow inputs the launcher stores.
`instructions_in_force.py`'s docstring has the judgment's fields; `transcript_judging.py`'s has
why every judge shares everything but its model and sampling;
`instructions_in_force_sequences.py`'s has what the sequences are for.

Both judged workflows judge with the one step `judge_transcript`. It takes the rubric and the
output schema as stored inputs, so a change to either is a change to its inputs, which the cache
sees. Each experiment keeps its own conversation, transcript and verdict steps, since they differ.

Every step that calls a model takes the model as two inputs: `model_name`, a string the step
builds the `ModelAPI` from, and `sampling`, a `SamplingParamsEntity`. A judgment does not record
its model, so the step's `model_name` input is the record of which model judged. In both judged
workflows, `model_name` and `sampling` are arrays over `[model]`, and `judge_model_name` and
`judge_sampling` arrays over `[judge]`, keyed by the specs the launcher was given. The judge's
instructions are the `judge_instructions` input, so "instructions" in an experiment's own inputs
means what the subject is asked to follow. `models.py`'s docstring has why sampling is an entity.
The project depends on `jsonschema` directly, which `behaviors.judging` needs.

`example_projects/example_project_testing.py`, beside this project, supplies the Postgres test
fixtures: a throwaway schema, and the project's declared modules loaded through the real client,
using fxtr's test `FakeEnvironment` to avoid pinning the working checkout. Workflow tests need
`FXTR_TEST_DATABASE_URL`; pure data and scoring tests run without a database, and provider calls
require the explicit `llm_integration` marker. The root pytest suite collects these tests, and they
also run and typecheck in the project's own environment, with lock and Pyright pre-commit hooks.
