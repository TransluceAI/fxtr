# Behaviors rollout examples

The project holds three experiments, each named for what it shows or asks:

- `echo` uses a deterministic EchoAPI to demonstrate independent two-turn conversations, with no
  provider calls.
- `instructions_in_force` asks whether a model keeps to the instructions still in force as a
  conversation sets, revises and withdraws them.
- `belief_reinforcement` asks whether supporting a distressed user strengthens their
  interpretation of events beyond the evidence. [`EXPERIMENT.md`](EXPERIMENT.md) describes it.

`instructions_in_force` holds multi-turn conversations and judges whether the model kept to the
instructions in force. Each of ten named sequences of user turns sets instructions early (a format, a
length, a language, words to avoid, facts to keep) and later tempts the model to break one, adds
or withdraws one, or makes a request that conflicts with one. Each model/sequence pair is a
separate fxtr step with its own conversation history, stored as a behaviors `ChatRollout`. Each
conversation is then turned into a `Transcript` and judged by behaviors' `judge`, by every judge
you name, several times each. A judgment's `match` is true when the assistant violated an
instruction in force, and its discussion cites the transcript by line. The workflow returns a
report of the conversations, transcripts, judgments and verdicts.

## Setup and launch

Each directory is an independent uv/fxtr project with its own lockfile and environment. Its
`[tool.fxtr]` lists the modules the CLI and viewer import; `[tool.fxtr.views]` points to its
`views/` bundle. The editable `fxtr` and `behaviors` dependencies come from this checkout.
From the project's directory:

```bash
uv sync --group dev
cp fxtr.local.toml.example fxtr.local.toml
```

The example local config uses the server in `../../postgres_env/`. Each project has a separate
schema, so its jobs and cache are independent. `FXTR_DATABASE_URL` can override the configured
server; put a different schema in `fxtr.local.toml` for scratch runs. Commit the code and lockfile
before launching: fxtr pins the checkout and verifies the project's environment. Launchers also
accept `--allow-staged` to pin staged changes and `--no-verify` to skip environment verification.
Credentials stay in the process environment.

Select Node using the [repository JavaScript setup](../../README.md#javascript-setup). Install JavaScript
dependencies with `pnpm --dir views install` from the project, after the core repository setup.
Commit `views/pnpm-lock.yaml` with the project. From the project:

```bash
uv run fxtr view    # in a second terminal; leave it running
```

`fxtr view` builds this project's renderers, serves the viewer with its jobs (in this checkout,
the viewer's dev server, from the repository root's `pnpm install`), and opens it in a browser.
While it runs, its open tabs switch to each job you launch (a new tab opens when none is open),
and saving a renderer updates the open viewer without a reload. Manage jobs with
`uv run fxtr jobs list`, `uv run fxtr jobs status JOB`, and `uv run fxtr resume JOB`; resume uses
the project's registered modules and stored inputs.

```bash
uv run python launch_echo.py
uv run python launch_instructions_in_force.py --model MODEL --model ANOTHER_MODEL --judge JUDGE_MODEL --judge JUDGE_MODEL:high
uv run python launch_instructions_in_force.py --model MODEL --judge JUDGE_MODEL --sequence never_say_party --judge-samples 1
uv run python launch_instructions_in_force.py --model MODEL --judge JUDGE_MODEL --turn "Answer in French from now on." --turn "What is a prime?"
```

The echo example needs Postgres but makes no provider calls. It also picks each
conversation's longest reply; open one in the viewer, and its link to the conversation marks that
reply there.

The instructions-in-force example uses a fresh cache namespace on each launch. `--model` and `--judge` are
repeatable. Each is `MODEL`, or `MODEL:EFFORT` with a reasoning effort (`low`, `high`, …), so one
model can be compared with itself at two efforts. A model is an OpenAI model unless it starts with
`anthropic/`. The launcher requires `OPENAI_API_KEY`, and `ANTHROPIC_API_KEY` when an Anthropic
model is named. Every judge gets the same instructions, rubric and output schema.
`--judge-samples` (3 by default) sets how many times each judge judges each conversation. `--sequence NAME` holds only the named sequences,
and `--turn` replaces them with one custom sequence of user turns. `--max-tokens` and
`--judge-max-tokens` cap output per reply and per judge answer, reasoning included, and
`--concurrency` caps the steps running at once. A run makes models × sequences conversations and
models × sequences × judges × samples judgments: two models, two judges and three samples over the
ten sequences is 20 conversations and 120 judgments.

The workflow's views, in the viewer's list of custom views:

- **overview**: how often each judge found a violation in each model's conversations, how often a
  judge's samples split, and every conversation with each judge's verdict in each sample. A
  verdict opens its judgment: its answer, each citation opening the transcript at the cited
  lines, and the judge's instructions, rubric, request and attempts.
- **conversation**: every conversation, grouped by sequence or model.
- **compare**: two models' replies to one sequence, aligned by turn.

## Belief reinforcement

`launch_belief_reinforcement.py` runs the matched cases described in
[`EXPERIMENT.md`](EXPERIMENT.md), with OpenAI or Anthropic subjects and judges.
Use `MODEL[:EFFORT]` for OpenAI and `anthropic/MODEL[:EFFORT]` for Anthropic, with exact IDs
available to your account. Set `OPENAI_API_KEY` and, for Anthropic calls, `ANTHROPIC_API_KEY`.

```bash
uv run python launch_belief_reinforcement.py --model MODEL --judge JUDGE --judge anthropic/ANTHROPIC_MODEL
uv run python launch_belief_reinforcement.py --model MODEL --judge JUDGE --situation friends --root belief-trial
```

By default all five situations and all three evidence conditions run, with two judgments per
judge per conversation. Repeat `--situation` or `--evidence weak|mixed|strong` to select cases.
`--judge-samples`, `--max-tokens`, `--judge-max-tokens` and `--concurrency` control the run.
The default cache root is fresh. Supply the same `--root` with `--overwrite-cache-conflicts`
to reuse a pilot's conversations and judgments while adding cases or judges with identical
settings. The summary recomputes from the expanded set of judgments. Entries whose settings
changed also recompute. This reuses existing samples. A new root draws fresh samples.
Standard `--allow-staged` and `--no-verify` apply.

The overview switches among reinforcement, emotional responsiveness and overall quality,
shows every sample, and filters to conversations with disagreement. Each verdict opens its
judgment and citations.

## Tests

The code and its tests are mapped in `CODE_OVERVIEW.md`. From this directory:

```bash
uv run --group dev pytest -n auto
uv run --group dev pyright
pnpm --dir views test
```
