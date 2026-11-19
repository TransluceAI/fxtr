# Joke evaluation

The example project of the `first-experiment` skill
([`.agents/skills/first-experiment/SKILL.md`](../../.agents/skills/first-experiment/SKILL.md)),
which walks a new user through their first fxtr experiment. It is the experiment that the skill's
guide, [Your first experiment](../../docs/fxtr/first-experiment.mdx), builds: which language
models write the funniest jokes? Each model writes jokes about everyday topics, a judge
model grades each joke from 1 to 10 with behaviors' `judge`, and a summary averages each model's
scores.

The guide's experiment module and launcher are this project's `src/joke_eval/experiment.py` and
`launch.py`, so the guide's code is tested here.

## Setup and launch

Set up the project as [`../README.md`](../README.md) describes, then put `ANTHROPIC_API_KEY` in
the environment. From this directory:

```bash
uv run fxtr view    # in a second terminal; leave it running
uv run python launch.py pilot
uv run python launch.py full
```

`pilot` runs 2 models × 3 topics × 1 sample (6 jokes and 6 judgments); `full` runs 10 topics with
3 samples each, reusing the pilot's results. The guide explains what each launch reuses.

## Tests

From this directory:

```bash
uv run --group dev pytest -n auto
uv run --group dev pyright
pnpm --dir views test
```
