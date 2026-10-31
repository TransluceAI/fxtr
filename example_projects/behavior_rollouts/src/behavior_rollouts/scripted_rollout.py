"""Run a scripted conversation inside a fxtr workflow without calling a model provider.

Run ``uv run python launch_scripted.py`` from the project directory. Substitute a provider
ModelAPI for EchoAPI to use the same rollout path with a real model.
"""

from typing import Annotated, cast

from behaviors.implementations.context_policies import (
    ScriptedUserSimulator,
    context_policy_from_user_simulator,
)
from behaviors.implementations.models import chat_session_model_from_api
from behaviors.rollouts.chat import run_chat_rollout
from behaviors.types import (
    AssistantMessage,
    ChatRollout,
    ContentText,
    ModelRequest,
    ModelResponse,
    SamplingParams,
    StopEndTurn,
    Usage,
    UserMessage,
)
from fxtr.core.entities import BoundID
from fxtr.experiment.handles import ArrayHandle
from fxtr.experiment.steps import StepContext, step
from fxtr.experiment.workflows import WorkflowContext, workflow


class EchoAPI:
    """A deterministic ModelAPI that echoes the latest user message."""

    async def generate(self, request: ModelRequest) -> ModelResponse:
        user = next(
            message for message in reversed(request.context) if isinstance(message, UserMessage)
        )
        text = "".join(block.text for block in user.content)
        return ModelResponse(
            id=f"echo-{len(request.context)}",
            output=(AssistantMessage(content=(ContentText(text=f"Echo: {text}"),)),),
            stop_reason=StopEndTurn(),
            usage=Usage(
                input_tokens=0,
                cache_read_input_tokens=None,
                cache_write_input_tokens=None,
                output_tokens=0,
                reasoning_output_tokens=None,
            ),
        )


@step(name="scripted_rollout.answer_prompt")
async def answer_prompt(context: StepContext, prompt: str) -> ChatRollout:
    """Roll out two scripted user turns; fxtr stores the returned ChatRollout."""
    model = chat_session_model_from_api(EchoAPI(), SamplingParams(max_tokens=100))
    user = ScriptedUserSimulator(
        messages=(
            UserMessage(content=(ContentText(text=prompt),)),
            UserMessage(content=(ContentText(text="Thank you."),)),
        )
    )
    policy = context_policy_from_user_simulator(user, initialization=None)
    return await run_chat_rollout(model, policy, max_turns=5)


@workflow(name="scripted_rollout.scripted_experiment")
async def scripted_experiment(
    context: WorkflowContext,
    prompts: Annotated[ArrayHandle[str], "[prompt: str]"],
) -> Annotated[ArrayHandle[BoundID[ChatRollout]], "[prompt: str]"]:
    """Map a behaviors rollout over prompts using the fxtr runner."""
    rollouts = context.run_step(
        "answer", answer_prompt, {"prompt": prompts}, map_over=["prompt"]
    ).expect_type(("[prompt: str]", BoundID[ChatRollout]))
    # Entity-returning steps store their results as references.
    return cast(ArrayHandle[BoundID[ChatRollout]], rollouts)
