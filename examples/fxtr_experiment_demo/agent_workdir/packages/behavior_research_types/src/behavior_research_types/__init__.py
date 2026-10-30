"""Vocabulary for the behavior-research demo scenario.

The marker (host-importable, no-deps) package holding everything the
scenario's grids, services, and renderers agree on: the entity types
(:mod:`.entities`), the three LLM service protocols (:mod:`.protocols`),
and the content-addressed handle keys (:mod:`.keys`). Service impls and
task fns live host-side in ``examples/fxtr_experiment_demo/behavior/``.
"""

from __future__ import annotations

from behavior_research_types.entities import (
    BEHAVIOR_RESEARCH_NS,
    AnalysisTable,
    ChatMessage,
    JudgeConfig,
    JudgeResult,
    RolloutConfig,
    ImprovementEvent,
    ServiceKey,
    SubjectModelConfig,
    SweepConfig,
    Transcript,
    UserSimConfig,
    UserSimImprovementRequest,
    UserSimImprovementResult,
)
from behavior_research_types.keys import (
    ANALYZE_TASK_KEY,
    ANTHROPIC_PROVIDER,
    IMPROVE_SIM_TASK_KEY,
    JUDGE_ANTHROPIC_KEY,
    JUDGE_MOCK_KEY,
    JUDGE_TASK_KEY,
    ROLLOUT_TASK_KEY,
    SUBJECT_ANTHROPIC_KEY,
    SUBJECT_MOCK_KEY,
    USER_SIM_ANTHROPIC_KEY,
    USER_SIM_MOCK_KEY,
)
from behavior_research_types.protocols import (
    BEHAVIOR_SERVICES_NS,
    JUDGE_MODEL_TYPE_ID,
    SUBJECT_MODEL_TYPE_ID,
    USER_SIM_TYPE_ID,
    JudgeModelProto,
    SubjectModelProto,
    UserSimProto,
)

__all__ = [
    "ANALYZE_TASK_KEY",
    "ANTHROPIC_PROVIDER",
    "IMPROVE_SIM_TASK_KEY",
    "BEHAVIOR_RESEARCH_NS",
    "BEHAVIOR_SERVICES_NS",
    "JUDGE_ANTHROPIC_KEY",
    "JUDGE_MOCK_KEY",
    "JUDGE_MODEL_TYPE_ID",
    "JUDGE_TASK_KEY",
    "ROLLOUT_TASK_KEY",
    "SUBJECT_ANTHROPIC_KEY",
    "SUBJECT_MOCK_KEY",
    "SUBJECT_MODEL_TYPE_ID",
    "USER_SIM_ANTHROPIC_KEY",
    "USER_SIM_MOCK_KEY",
    "USER_SIM_TYPE_ID",
    "AnalysisTable",
    "ChatMessage",
    "JudgeConfig",
    "JudgeResult",
    "JudgeModelProto",
    "RolloutConfig",
    "ImprovementEvent",
    "ServiceKey",
    "SubjectModelConfig",
    "SubjectModelProto",
    "SweepConfig",
    "Transcript",
    "UserSimConfig",
    "UserSimImprovementRequest",
    "UserSimImprovementResult",
    "UserSimProto",
]
