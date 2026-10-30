"""The behavior scenario's workflow runner mount.

:func:`mount_behavior_runner` is the behavior twin of
:func:`experiment_runner.mount_experiment_runner` — the same
load-bearing scope topology (see that module's docstring: non-isolated
host scope *enclosing* the session + runner scope) over its own sqlite
pair, auto-binding the behavior task fns plus, per ``llm_mode``, the
LLM services:

- ``"real"`` — one shared :class:`anthropic_client.AnthropicChat`
  (owned by the mount, closed on unwind) behind the real subject /
  user-sim / judge impls, bound under the ``:anthropic`` keys.
- ``"mock"`` — the :mod:`mock_llm_services` impls under the distinct
  ``:mock`` keys (a mode switch rewrites handle cells → staleness).
- ``"off"`` — no LLM bindings at all; the key table still carries the
  ``:anthropic`` keys, so structure built in this mode names the real
  services and demands fault with the boundness error until a
  binding-capable relaunch.

The yielded :class:`BehaviorRunner` extends the generic runner handle
with the mode's key table: ``fn_keys`` (task fns) + ``svc_keys``
(``subject_model`` / ``user_sim`` / ``judge_model``) hold exactly the
content-addressed key ids this mount would bind, and the driver writes
handle cells from them — cells always name what the current mode
serves. Run it under an :class:`experiment_runner.RunnerSupervisor`
via ``RunnerSupervisor(data_dir, mount=..., external_name=...)``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator, Literal

from behavior_research_types import keys as k
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope, ServiceRef
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr_experiment.executor import Executor
from fxtr_experiment.state_store import WorkflowSession
from fxtr_experiment.bindings import BindingHost, wrap_singleton
from fxtr_experiment.user_api import UserCapabilityProto
from fxtr_experiment.user_service import UserCapabilityImpl, Workflow

from analyze_task import analyze_task
from experiment_runner import ExperimentRunner
from improve_sim_task import improve_sim_task
from judge_task import judge_task
from rollout_task import rollout_task

if TYPE_CHECKING:
    from behavior_research_types.entities import ServiceKey

LlmMode = Literal["off", "mock", "real"]

#: The task-fn adapters this mount always binds, by key-table name.
_TASK_FNS = {
    "rollout": (k.ROLLOUT_TASK_KEY, rollout_task),
    "judge": (k.JUDGE_TASK_KEY, judge_task),
    "analyze": (k.ANALYZE_TASK_KEY, analyze_task),
    "improve_sim": (k.IMPROVE_SIM_TASK_KEY, improve_sim_task),
}


@dataclasses.dataclass
class BehaviorRunner(ExperimentRunner):
    """The behavior runner handle: the generic handle + the key table.

    Attributes:
        svc_keys: Handle-key entity id per LLM service slot
            (``subject_model`` / ``user_sim`` / ``judge_model``),
            mode-appropriate (stored in ``scope``, like ``fn_keys``).
        llm_mode: The mode this mount was opened with.
    """

    svc_keys: dict[str, FxtrID]
    llm_mode: LlmMode


def _llm_slots(llm_mode: LlmMode) -> "dict[str, ServiceKey]":
    """The mode's LLM key table (off names the real services)."""
    if llm_mode == "mock":
        return {
            "subject_model": k.SUBJECT_MOCK_KEY,
            "user_sim": k.USER_SIM_MOCK_KEY,
            "judge_model": k.JUDGE_MOCK_KEY,
        }
    return {
        "subject_model": k.SUBJECT_ANTHROPIC_KEY,
        "user_sim": k.USER_SIM_ANTHROPIC_KEY,
        "judge_model": k.JUDGE_ANTHROPIC_KEY,
    }


@asynccontextmanager
async def mount_behavior_runner(
    data_dir: Path, *, llm_mode: LlmMode = "off", anthropic_api_key: str | None = None
) -> AsyncIterator[BehaviorRunner]:
    """Open the behavior runner over ``data_dir`` (see module docstring).

    Args:
        data_dir: Directory for the two sqlite files (created if
            missing).
        llm_mode: Which LLM services to bind (module docstring).
        anthropic_api_key: Required iff ``llm_mode == "real"``.
    """
    if llm_mode == "real" and not anthropic_api_key:
        raise ValueError("llm_mode='real' requires an anthropic_api_key")
    data_dir.mkdir(parents=True, exist_ok=True)
    # Topology per experiment_runner's module docstring (non-isolated
    # host enclosing session + runner scope; both properties load-bearing).
    async with contextlib.AsyncExitStack() as stack:
        host_scope = await stack.enter_async_context(FxtrScope(name="behavior-demo-host"))
        session = await stack.enter_async_context(
            WorkflowSession.open(str(data_dir / "state.sqlite"), str(data_dir / "entities.sqlite"))
        )
        runner_scope = await stack.enter_async_context(
            FxtrScope(
                name="behavior-runner",
                isolated=True,
                # GLOBAL_SCOPE: service type entities (define_type) live there.
                include=[session.entity_scope, GLOBAL_SCOPE],
            )
        )
        bindings = BindingHost(runner_scope, session)
        executor = Executor(session, bindings, runner_scope)
        impl = UserCapabilityImpl(session, executor, bindings, runner_scope)
        cap_ref = await runner_scope.mount(impl)
        # Membership: mounted on (thus resolvable in) the runner scope.
        proxy = await cap_ref.replace_service_type(UserCapabilityProto).resolve(runner_scope)
        wf = await Workflow.connect(proxy)

        releases: list[ServiceRef] = []

        async def bind(key_entity: "ServiceKey", service: TypedServiceImpl) -> FxtrID:
            """Mount on the host scope, bind under the key; return the key id.

            Key entities store into the runner scope (call-arg coverage
            — see ExperimentRunner's class docstring); the connector
            mount lives on the enclosing host scope, dying with the
            runner (bindings are per-mount ephemera).
            """
            key = key_entity.store(runner_scope).entity_id
            svc_ref = await host_scope.mount(service)
            connector = await wrap_singleton(host_scope, svc_ref)
            releases.append(await wf.bind_handle(key, connector))
            return key

        fn_keys = {name: await bind(entity, fn) for name, (entity, fn) in _TASK_FNS.items()}

        slots = _llm_slots(llm_mode)
        svc_keys = {name: entity.store(runner_scope).entity_id for name, entity in slots.items()}
        if llm_mode == "mock":
            from mock_llm_services import MockJudge, MockSubjectModel, MockUserSim

            await bind(slots["subject_model"], MockSubjectModel())
            await bind(slots["user_sim"], MockUserSim())
            await bind(slots["judge_model"], MockJudge())
        elif llm_mode == "real":
            from anthropic_client import AnthropicChat
            from judge_service import JudgeImpl
            from subject_model_service import SubjectModelImpl
            from user_sim_service import UserSimImpl

            assert anthropic_api_key is not None
            chat = AnthropicChat(api_key=anthropic_api_key)
            stack.push_async_callback(chat.aclose)
            await bind(slots["subject_model"], SubjectModelImpl(chat))
            await bind(slots["user_sim"], UserSimImpl(chat))
            await bind(slots["judge_model"], JudgeImpl(chat))

        yield BehaviorRunner(
            capability_ref=cap_ref,
            scope=runner_scope,
            host_scope=host_scope,
            workflow=wf,
            session=session,
            fn_keys=fn_keys,
            fn_instances={},
            releases=releases,
            svc_keys=svc_keys,
            llm_mode=llm_mode,
        )


def behavior_mount_factory(
    *, llm_mode: LlmMode, anthropic_api_key: str | None
) -> "functools.partial[contextlib.AbstractAsyncContextManager[BehaviorRunner]]":
    """The ``RunnerSupervisor``-shaped mount factory for one mode."""
    return functools.partial(
        mount_behavior_runner, llm_mode=llm_mode, anthropic_api_key=anthropic_api_key
    )
