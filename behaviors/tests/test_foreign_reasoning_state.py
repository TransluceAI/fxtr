"""Reasoning whose state the destination does not accept is removed from a request before any
codec renders it (`_common.without_foreign_reasoning_state`), and named for the run's report."""

from behaviors.implementations.dialects._common import (
    foreign_reasoning_state,
    same_openrouter_model,
    without_foreign_reasoning_state,
)
from behaviors.implementations.dialects.anthropic_messages import (
    AnthropicMessagesCodec,
    AnthropicSignature,
)
from behaviors.implementations.dialects.codec_utils import own_reasoning_state
from behaviors.implementations.dialects.openai_responses import OpenAIResponsesReasoning
from behaviors.types.content import ContentReasoning, ContentText
from behaviors.types.message import AssistantMessage, UserMessage
from behaviors.types.response import ModelRequest, SamplingParams
from fxtr.entity_defns.dynamic_data import DynamicData

_OWN = DynamicData.from_tagged(OpenAIResponsesReasoning(id="rs_1", encrypted_content="enc"))
_OTHER = DynamicData.from_tagged(AnthropicSignature(signature="EqQBCkYIBxgC"))


def _accepts(state: DynamicData) -> bool:
    return state.is_type(OpenAIResponsesReasoning)


def _request(*states: DynamicData) -> ModelRequest:
    return ModelRequest(
        context=(
            UserMessage(content=(ContentText(text="hi"),)),
            AssistantMessage(
                content=(
                    *(
                        ContentReasoning(text="hm", form="summary", reasoning_state=s)
                        for s in states
                    ),
                    ContentText(text="ok"),
                )
            ),
        ),
        tools=(),
        tool_choice=None,
        parallel_tool_calls=None,
        sampling=SamplingParams(),
    )


def test_a_block_whose_state_the_destination_rejects_is_removed_and_named() -> None:
    request = _request(_OTHER, _OWN)
    assert foreign_reasoning_state(request, _accepts) == (_OTHER.type,)
    stripped, rejected = without_foreign_reasoning_state(request, _accepts)
    assert rejected == (_OTHER.type,)
    assistant = stripped.context[1]
    assert isinstance(assistant, AssistantMessage)
    assert assistant.content == (
        ContentReasoning(text="hm", form="summary", reasoning_state=_OWN),
        ContentText(text="ok"),
    )


def test_a_request_with_nothing_rejected_is_returned_as_is() -> None:
    request = _request(_OWN)
    assert without_foreign_reasoning_state(request, _accepts) == (request, ())


def test_routing_variants_and_unreported_models_count_as_the_same_model() -> None:
    assert same_openrouter_model("moonshotai/kimi-k3", "moonshotai/kimi-k3:nitro")
    assert same_openrouter_model("moonshotai/kimi-k3:floor", "moonshotai/kimi-k3")
    assert same_openrouter_model("", "anything")
    assert not same_openrouter_model("moonshotai/kimi-k3", "z-ai/glm-5.3")


def test_a_codec_rejects_its_own_tag_on_a_state_that_does_not_decode_as_its_type() -> None:
    # Reasoning state comes back through harnesses as tagged JSON, which can carry a codec's tag
    # with fields that are not its type's.
    malformed = DynamicData.from_data({"$type": AnthropicSignature.fxtr_type_id(), "signature": 3})
    assert own_reasoning_state(malformed, AnthropicSignature) is None
    assert not AnthropicMessagesCodec().accepts_reasoning_state(malformed, "claude-x")
    assert AnthropicMessagesCodec().accepts_reasoning_state(_OTHER, "claude-x")
