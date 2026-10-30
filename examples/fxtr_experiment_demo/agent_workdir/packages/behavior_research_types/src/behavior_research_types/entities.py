"""Entity types for the behavior-research scenario's workflow values.

Every value the scenario's grids hold is one of these types, so cell
contents render as something readable in the browser once the workdir's
renderer slices route them. The namespace UUID is stable so the types'
wire identities don't change as long as the label strings stay put.

Derived entities carry **provenance refs** (plain ``EntityRef`` fields
— related inputs, not owned children; either way the referenced
entities join the closure): a :class:`Transcript` links the configs
that produced it, a :class:`JudgeResult` links its rubric and the
transcript it judged, and an :class:`AnalysisTable` links, per row,
every judgement the row aggregates. This is deliberately *in-data*
provenance: the workflow's generation records already track what each
cell read, but refs inside the entities travel with the data — they
survive export and let renderers trace any object back to its inputs
with plain links.

Score convention: :class:`JudgeResult.score` is in ``[0, 1]`` (1 =
"the rubric's behavior is maximally present"); the analysis reports
means over sims x replicas per (subject model, judge).
"""

from __future__ import annotations

import dataclasses
import uuid

from fxtr2.conversion.converters.mashumaro_entities import Child, DataclassEntity
from fxtr2.conversion.converters.unknown_entity import UnknownEntity
from fxtr2.data import system_entities
from fxtr2.data.entity_ref_scope import EntityRef

#: Stable namespace UUID for the behavior-research entity types.
BEHAVIOR_RESEARCH_NS = uuid.UUID("ed1c82cc-d8ee-482b-a501-5db8134fdacc")


def _type_id(label: str):
    return system_entities.define_type(BEHAVIOR_RESEARCH_NS, label)


TRANSCRIPT_TYPE_ID = _type_id("transcript")
USER_SIM_IMPROVEMENT_REQUEST_TYPE_ID = _type_id("user_sim_improvement_request")
IMPROVEMENT_EVENT_TYPE_ID = _type_id("improvement_event")
USER_SIM_IMPROVEMENT_RESULT_TYPE_ID = _type_id("user_sim_improvement_result")
SUBJECT_MODEL_CONFIG_TYPE_ID = _type_id("subject_model_config")
USER_SIM_CONFIG_TYPE_ID = _type_id("user_sim_config")
JUDGE_CONFIG_TYPE_ID = _type_id("judge_config")
ROLLOUT_CONFIG_TYPE_ID = _type_id("rollout_config")
SWEEP_CONFIG_TYPE_ID = _type_id("sweep_config")
JUDGE_RESULT_TYPE_ID = _type_id("judge_result")
ANALYSIS_TABLE_TYPE_ID = _type_id("analysis_table")
SERVICE_KEY_TYPE_ID = _type_id("service_key")


# -- configs (the leaf inputs; defined first so derived types can ref them) ----


@dataclasses.dataclass(frozen=True)
class SubjectModelConfig(DataclassEntity, fxtr_type=SUBJECT_MODEL_CONFIG_TYPE_ID):
    """One subject model under study.

    ``provider`` names the coordinate of ``services/
    subject_model_services`` whose handle serves this config's calls;
    ``model`` is the provider's model id; ``system_prompt`` is the
    assistant's system prompt (the thing this demo sweeps).
    """

    provider: str
    model: str
    system_prompt: str


@dataclasses.dataclass(frozen=True)
class UserSimConfig(DataclassEntity, fxtr_type=USER_SIM_CONFIG_TYPE_ID):
    """One simulated-user persona.

    ``task_prompt`` describes the overall simulation task (shared
    framing); ``persona`` describes this particular simulated user.
    The simulator's model is fixed by the bound user-simulator service,
    deliberately not config — the sim is not a sweep axis in this demo.
    """

    task_prompt: str
    persona: str


@dataclasses.dataclass(frozen=True)
class JudgeConfig(DataclassEntity, fxtr_type=JUDGE_CONFIG_TYPE_ID):
    """One judge: a rubric plus the provider/model that scores it.

    ``provider`` names the coordinate of ``services/judge_services``
    whose handle serves this config's calls.
    """

    provider: str
    model: str
    rubric: str


@dataclasses.dataclass(frozen=True)
class RolloutConfig(DataclassEntity, fxtr_type=ROLLOUT_CONFIG_TYPE_ID):
    """Rollout shape shared by every rollout cell.

    Deliberately separate from :class:`SweepConfig`: replica-count
    edits must not staleness-propagate into already-computed rollouts.
    ``max_turns`` counts user/assistant exchanges (one turn = one user
    message + one assistant reply).
    """

    max_turns: int


@dataclasses.dataclass(frozen=True)
class SweepConfig(DataclassEntity, fxtr_type=SWEEP_CONFIG_TYPE_ID):
    """Sweep shape read by the analysis: how many replicas to span."""

    replicas: int


# -- derived values (each carries provenance refs to its inputs) ----------------


@dataclasses.dataclass(frozen=True)
class ChatMessage:
    """One turn of a conversation (plain nested value, not an entity).

    ``role`` is ``"user"`` or ``"assistant"``.
    """

    role: str
    text: str


@dataclasses.dataclass(frozen=True)
class Transcript(DataclassEntity, fxtr_type=TRANSCRIPT_TYPE_ID):
    """A finished rollout: the alternating user/assistant messages,
    plus refs to the configs the conversation was produced from."""

    messages: tuple[ChatMessage, ...]
    subject_config: EntityRef[SubjectModelConfig]
    sim_config: EntityRef[UserSimConfig]
    rollout_config: EntityRef[RolloutConfig]


@dataclasses.dataclass(frozen=True)
class JudgeResult(DataclassEntity, fxtr_type=JUDGE_RESULT_TYPE_ID):
    """One judge's verdict on one transcript (``score`` in [0, 1]),
    plus refs to the rubric applied and the transcript judged."""

    score: float
    reasoning: str
    judge_config: EntityRef[JudgeConfig]
    transcript: EntityRef[Transcript]


@dataclasses.dataclass(frozen=True)
class AnalysisTable(DataclassEntity, fxtr_type=ANALYSIS_TABLE_TYPE_ID):
    """A JSON-shaped table of analysis results.

    Deliberately generic (named columns + uniform rows) so one table
    renderer covers future analyses; cells are strings or numbers.
    Every row must have exactly ``len(columns)`` cells.

    ``row_sources`` is parallel to ``rows``: ``row_sources[i]`` refs
    every :class:`JudgeResult` that row ``i``'s aggregate was computed
    from (the per-row provenance the renderer traces through).
    """

    columns: tuple[str, ...]
    rows: tuple[tuple[str | int | float, ...], ...]
    row_sources: tuple[tuple[EntityRef[JudgeResult], ...], ...]


# -- the user-sim improvement transformation (ABR-style content rewrite) --------


@dataclasses.dataclass(frozen=True)
class UserSimImprovementRequest(DataclassEntity, fxtr_type=USER_SIM_IMPROVEMENT_REQUEST_TYPE_ID):
    """A pinned-snapshot request to improve one user-sim persona.

    Everything the transformation consumes travels by ref *inside*
    this entity — configs included — so the request is deliberately
    detached from the live ``configs/`` grids (editing the original
    sim later does not affect a past transformation). ``rollouts`` and
    ``judgements`` are parallel: ``judgements[i]`` scored
    ``rollouts[i]``. The improver model (``improver_provider`` /
    ``improver_model``) is the LLM asked to rewrite the persona.
    """

    instruction: str
    improver_provider: str
    improver_model: str
    sim_config: EntityRef[UserSimConfig]
    subject_config: EntityRef[SubjectModelConfig]
    judge_config: EntityRef[JudgeConfig]
    rollout_config: EntityRef[RolloutConfig]
    rollouts: tuple[EntityRef[Transcript], ...]
    judgements: tuple[EntityRef[JudgeResult], ...]


@dataclasses.dataclass(frozen=True)
class ImprovementEvent(DataclassEntity, fxtr_type=IMPROVEMENT_EVENT_TYPE_ID):
    """One entry of a transformation's chronological event log.

    ``kind`` names the action (``"request"`` / ``"proposal"`` /
    ``"trial_transcript"`` / ``"trial_judgement"`` /
    ``"final_choice"``); ``text`` is the actor's words for the event
    (the instruction, the improver's notes — empty when the subject
    speaks for itself); ``subject`` refs the object the event is about
    (typed loads go through ``replace_target_type``; renderers dispatch
    on the subject's actual type).
    """

    kind: str
    text: str
    subject: EntityRef[UnknownEntity] | None = None


@dataclasses.dataclass(frozen=True)
class UserSimImprovementResult(DataclassEntity, fxtr_type=USER_SIM_IMPROVEMENT_RESULT_TYPE_ID):
    """The transformation's output: the chosen bio plus the whole
    improvement trajectory as a chronological **event log** (request →
    proposal → trial transcript → trial judgement → … → final choice;
    ``Child`` entries load inline). ``final_bio`` may equal an earlier
    proposal's bio — the improver is allowed to stand by it."""

    request: EntityRef[UserSimImprovementRequest]
    events: tuple[Child[ImprovementEvent], ...]
    final_bio: EntityRef[UserSimConfig]
    final_notes: str


@dataclasses.dataclass(frozen=True)
class ServiceKey(DataclassEntity, fxtr_type=SERVICE_KEY_TYPE_ID):
    """A content-addressed handle-key entity (see ``keys.py``).

    Handle cells hold these; the *id* is the identity the workflow
    observes (and staleness tracks), the fields are what a human sees
    when opening the cell.
    """

    label: str
    detail: str
