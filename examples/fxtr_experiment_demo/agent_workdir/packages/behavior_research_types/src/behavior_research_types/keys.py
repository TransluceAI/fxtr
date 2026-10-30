"""Content-addressed handle keys for the behavior-research scenario.

Handle cells hold :class:`~behavior_research_types.entities.ServiceKey`
entities; a key's *id* is what the workflow observes when a task reads
the handle, so **which key** is bound is the trust/staleness boundary.
Content-addressing makes each key's id stable across relaunches (the
runner mount's auto-bind lines up with handle cells written in earlier
sessions), and the real vs mocked LLM services carry **distinct keys**
on purpose: switching mode rewrites the handle cells, which fans
staleness through everything computed against the other mode's
services (mock results are never silently served as real ones).

The task-fn keys are mode-independent (the same host code runs in
every mode).
"""

from __future__ import annotations

from behavior_research_types.entities import ServiceKey

#: The provider coordinate the demo's configs name. Mode-independent:
#: mock services bind under the same coordinate with a different key
#: (the coordinate is an address; the key entity is the identity).
ANTHROPIC_PROVIDER = "anthropic"

# -- task fns (mode-independent) ----------------------------------------------

ROLLOUT_TASK_KEY = ServiceKey(
    label="task: rollout",
    detail="runs one subject-model x user-sim conversation and stores its Transcript",
)
JUDGE_TASK_KEY = ServiceKey(
    label="task: judge",
    detail="scores one Transcript against one judge rubric, storing a JudgeResult",
)
ANALYZE_TASK_KEY = ServiceKey(
    label="task: analyze",
    detail="aggregates judgements over the active domain into an AnalysisTable",
)
IMPROVE_SIM_TASK_KEY = ServiceKey(
    label="task: improve_sim",
    detail=(
        "the user-sim improvement flow: proposes persona rewrites and trials "
        "each with nested rollout+judge runs in its own internals"
    ),
)

# -- LLM services, real (anthropic) mode --------------------------------------

SUBJECT_ANTHROPIC_KEY = ServiceKey(
    label="service: subject model (anthropic)",
    detail="chat completion via the Anthropic API",
)
USER_SIM_ANTHROPIC_KEY = ServiceKey(
    label="service: user simulator (anthropic)",
    detail="simulated-user turns via the Anthropic API",
)
JUDGE_ANTHROPIC_KEY = ServiceKey(
    label="service: judge model (anthropic)",
    detail="rubric judging via the Anthropic API",
)

# -- LLM services, mocked mode -------------------------------------------------

SUBJECT_MOCK_KEY = ServiceKey(
    label="service: subject model (MOCK)",
    detail="canned deterministic replies — obviously fake, for bring-up and tests",
)
USER_SIM_MOCK_KEY = ServiceKey(
    label="service: user simulator (MOCK)",
    detail="canned deterministic persona turns — obviously fake, for bring-up and tests",
)
JUDGE_MOCK_KEY = ServiceKey(
    label="service: judge model (MOCK)",
    detail="hash-derived deterministic verdicts — obviously fake, for bring-up and tests",
)
