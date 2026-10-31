"""Tests for DocentReadingStep and the Docent type conversions."""

import anyio
import pytest
from docent._llm_util.data_models.exceptions import (  # pyright: ignore[reportPrivateImportUsage]
    ValidationFailedException,
)
from docent.data_models.chat import AssistantMessage as DocentAssistantMessage
from docent.data_models.chat import ToolMessage as DocentToolMessage

from behaviors.implementations.docent import (
    DocentReadingStep,
    DocentReadingStepConfig,
    DocentReadingStepResult,
    to_docent_agent_run,
    to_docent_message,
)
from behaviors.types import (
    AgentRun,
    AssistantMessage,
    CacheableContentText,
    CacheControl,
    ContentReasoning,
    ContentRedactedReasoning,
    ContentSummarizedReasoning,
    ContentText,
    ContentToolUse,
    JSONObject,
    JSONValue,
    ModelCallFailure,
    ModelRequest,
    ModelResponse,
    SamplingParams,
    StopEndTurn,
    SystemMessage,
    ToolMessage,
    Transcript,
    TranscriptGroup,
    Usage,
    UserMessage,
)
from fxtr.storage.memory_storage import MemoryStorage


def _response(text: str) -> ModelResponse:
    return ModelResponse(
        id="msg_1",
        output=(AssistantMessage(content=(ContentText(text=text),)),),
        stop_reason=StopEndTurn(),
        usage=Usage(
            input_tokens=1,
            cache_read_input_tokens=None,
            cache_write_input_tokens=None,
            output_tokens=1,
            reasoning_output_tokens=None,
        ),
    )


class ScriptedModelAPI:
    """Returns the given responses/failures, one per call, in order."""

    def __init__(self, outputs: tuple[ModelResponse | ModelCallFailure, ...]):
        self._outputs = list(outputs)
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse | ModelCallFailure:
        self.requests.append(request)
        return self._outputs.pop(0)


def make_transcript(*texts: tuple[str, str]) -> Transcript:
    messages: list[SystemMessage | UserMessage | AssistantMessage] = []
    for role, text in texts:
        if role == "user":
            messages.append(UserMessage(content=(ContentText(text=text),)))
        else:
            messages.append(AssistantMessage(content=(ContentText(text=text),)))
    return Transcript(name=None, description=None, messages=tuple(messages))


SCHEMA: JSONObject = {
    "type": "object",
    "properties": {"verdict": {"type": "string"}},
    "required": ["verdict"],
    "additionalProperties": False,
}
DEFAULT_SCHEMA: JSONObject = {
    "type": "object",
    "properties": {"output": {"type": "string"}},
}


def make_segments(
    param_type: str = "transcript", extra: tuple[JSONValue, ...] = ()
) -> tuple[JSONValue, ...]:
    return (
        "Evaluate the following:\n",
        {"param_name": "the_input", "param_type": param_type, "is_list": False},
        *extra,
        "\nRespond as instructed.",
    )


def make_step(
    outputs: tuple[ModelResponse | ModelCallFailure, ...],
    schema: JSONObject = SCHEMA,
    segments: tuple[JSONValue, ...] | None = None,
    **config_kwargs: JSONValue,
) -> tuple[DocentReadingStep, ScriptedModelAPI]:
    config = DocentReadingStepConfig(
        prompt_template_segments=segments if segments is not None else make_segments(),
        output_schema=schema,
        input_param_name="the_input",
        **config_kwargs,  # pyright: ignore[reportArgumentType]
    )
    api = ScriptedModelAPI(outputs)
    return DocentReadingStep(config=config, model_api=api, sampling=SamplingParams()), api


def _prompt_text(request: ModelRequest) -> str:
    user = request.context[1]
    return "\n".join(block.text for block in user.content if isinstance(block, ContentText))


def _system_text(request: ModelRequest) -> str:
    system = request.context[0]
    return "\n".join(block.text for block in system.content if isinstance(block, ContentText))


def test_renders_transcript_into_prompt_and_parses_response() -> None:
    step, api = make_step(outputs=(_response('{"verdict": "correct"}'),))
    transcript = make_transcript(("user", "What is 2+2?"), ("assistant", "4"))

    result = anyio.run(step.read, transcript)

    assert isinstance(result, DocentReadingStepResult)
    assert result.parsed_response == {"verdict": "correct"}
    assert result.retry_error_transcripts == ()
    assert result.reader_transcript is not None
    assert [type(m).__name__ for m in result.reader_transcript.messages] == [
        "SystemMessage",
        "UserMessage",
        "AssistantMessage",
    ]

    prompt = _prompt_text(api.requests[0])
    assert "Evaluate the following:" in prompt
    assert "What is 2+2?" in prompt and "4" in prompt
    assert "Respond as instructed." in prompt
    assert "JSON schema" in _system_text(api.requests[0])


def test_default_output_schema_wraps_raw_text() -> None:
    step, api = make_step(outputs=(_response("just some text"),), schema=DEFAULT_SCHEMA)

    result = anyio.run(step.read, make_transcript(("user", "hi")))

    assert isinstance(result, DocentReadingStepResult)
    assert result.parsed_response == {"output": "just some text"}
    assert "JSON schema" not in _system_text(api.requests[0])


def test_extra_text_param_substitution() -> None:
    segments = make_segments(
        extra=("\nStyle: ", {"param_name": "style", "param_type": "text", "is_list": False})
    )
    step, api = make_step(
        outputs=(_response('{"verdict": "ok"}'),),
        segments=segments,
        extra_text_params={"style": "terse"},
    )

    anyio.run(step.read, make_transcript(("user", "hi")))

    prompt = _prompt_text(api.requests[0])
    assert "Style:" in prompt and "terse" in prompt


def test_extra_reading_result_param_renders_json() -> None:
    segments = make_segments(
        extra=("\nPrior: ", {"param_name": "prior", "param_type": "reading_result"})
    )
    step, api = make_step(
        outputs=(_response('{"verdict": "ok"}'),),
        segments=segments,
        extra_reading_results={"prior": {"suspicious": True}},
    )

    anyio.run(step.read, make_transcript(("user", "hi")))

    assert '"suspicious"' in _prompt_text(api.requests[0])


def test_system_prefix_override_replaces_default_prefix() -> None:
    step, api = make_step(
        outputs=(_response('{"verdict": "ok"}'),),
        system_prefix_override="You are a strict grader.",
    )

    anyio.run(step.read, make_transcript(("user", "hi")))

    assert _system_text(api.requests[0]).startswith("You are a strict grader.")


def test_citation_fields_resolve_and_plain_fields_strip_markup() -> None:
    schema: JSONObject = {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "citations": True},
            "summary": {"type": "string"},
        },
        "required": ["verdict", "summary"],
    }
    step, api = make_step(
        outputs=(
            _response('{"verdict": "Correct [T0B1].", "summary": "The answer is right [T0B1]."}'),
        ),
        schema=schema,
    )

    result = anyio.run(step.read, make_transcript(("user", "What is 2+2?"), ("assistant", "4")))

    assert "citation" in _system_text(api.requests[0]).lower()
    assert isinstance(result, DocentReadingStepResult)
    verdict = result.parsed_response["verdict"]
    assert isinstance(verdict, dict)
    verdict_text = verdict["text"]
    assert isinstance(verdict_text, str) and "Correct" in verdict_text
    citations = verdict["citations"]
    assert isinstance(citations, list) and len(citations) == 1
    assert result.parsed_response["summary"] == "The answer is right."


def test_invalid_output_resamples_then_succeeds() -> None:
    step, api = make_step(outputs=(_response("not json"), _response('{"verdict": "ok"}')))

    result = anyio.run(step.read, make_transcript(("user", "hi")))

    assert isinstance(result, DocentReadingStepResult)
    assert result.parsed_response == {"verdict": "ok"}
    assert len(api.requests) == 2
    assert len(result.retry_error_transcripts) == 1
    assert result.retry_error_transcripts[0].metadata["is_retry_error"] is True


def test_invalid_output_exhausted_returns_default() -> None:
    step, _ = make_step(
        outputs=(_response("nope"), _response("still nope")),
        max_output_attempts=2,
        default_parsed_response_on_failure={"verdict": "unknown"},
    )

    result = anyio.run(step.read, make_transcript(("user", "hi")))

    assert isinstance(result, DocentReadingStepResult)
    assert result.parsed_response == {"verdict": "unknown"}
    assert result.reader_transcript is None
    assert len(result.retry_error_transcripts) == 2


def test_invalid_output_exhausted_raises_without_default() -> None:
    step, _ = make_step(outputs=(_response("nope"),), max_output_attempts=1)

    with pytest.raises(ValidationFailedException):
        anyio.run(step.read, make_transcript(("user", "hi")))


def test_transport_failure_returned_as_data() -> None:
    failure = ModelCallFailure(category="rate_limited", description="429", provider_http_status=429)
    step, _ = make_step(outputs=(failure,))

    result = anyio.run(step.read, make_transcript(("user", "hi")))

    assert result == failure


def test_agent_run_input_renders_all_transcripts() -> None:
    step, api = make_step(
        outputs=(_response('{"verdict": "ok"}'),), segments=make_segments(param_type="agent_run")
    )
    run = AgentRun(
        name="run",
        description=None,
        transcripts=(make_transcript(("user", "top-level marker")),),
        groups=(
            TranscriptGroup(
                name="judges",
                description=None,
                transcripts=(make_transcript(("user", "grouped marker")),),
                groups=(),
            ),
        ),
    )

    result = anyio.run(step.read, run)

    assert isinstance(result, DocentReadingStepResult)
    prompt = _prompt_text(api.requests[0])
    assert "top-level marker" in prompt
    assert "grouped marker" in prompt


def test_input_kind_mismatch_raises() -> None:
    transcript_step, _ = make_step(outputs=())
    with pytest.raises(ValueError, match="AgentRun"):
        anyio.run(
            transcript_step.read,
            AgentRun(name=None, description=None, transcripts=(), groups=()),
        )

    agent_run_step, _ = make_step(outputs=(), segments=make_segments(param_type="agent_run"))
    with pytest.raises(ValueError, match="Transcript"):
        anyio.run(agent_run_step.read, make_transcript(("user", "hi")))


def _config(segments: tuple[JSONValue, ...], **kwargs: JSONValue) -> DocentReadingStepConfig:
    return DocentReadingStepConfig(
        prompt_template_segments=segments,
        output_schema=SCHEMA,
        input_param_name="the_input",
        **kwargs,  # pyright: ignore[reportArgumentType]
    )


def test_config_rejects_unsupported_param_type() -> None:
    with pytest.raises(ValueError, match="transcript_slice"):
        _config(make_segments(extra=({"param_name": "slice", "param_type": "transcript_slice"},)))


def test_config_rejects_unbound_text_param() -> None:
    with pytest.raises(ValueError, match="no binding"):
        _config(make_segments(extra=({"param_name": "style", "param_type": "text"},)))


def test_config_rejects_missing_input_binding() -> None:
    with pytest.raises(ValueError, match="no placeholder"):
        _config(("no placeholders here",))


def test_config_rejects_second_transcript_param() -> None:
    with pytest.raises(ValueError, match="additional transcript"):
        _config(make_segments(extra=({"param_name": "other", "param_type": "transcript"},)))


def test_config_accepts_unknown_input_type() -> None:
    config = _config(
        (
            "Read this:\n",
            {"param_name": "the_input", "param_type": "unknown", "is_list": False},
        )
    )
    assert config.input_param_name == "the_input"


def test_to_docent_message_maps_tool_use_and_errors() -> None:
    assistant = AssistantMessage(
        content=(
            ContentText(text="calling a tool"),
            ContentToolUse(id="tu_1", name="search", input={"q": "cats"}, parse_error=None),
        )
    )
    docent_assistant = to_docent_message(assistant)
    assert isinstance(docent_assistant, DocentAssistantMessage)
    assert docent_assistant.tool_calls is not None
    assert docent_assistant.tool_calls[0].function == "search"
    assert docent_assistant.tool_calls[0].arguments == {"q": "cats"}

    tool = ToolMessage(content=(ContentText(text="boom"),), tool_call_id="tu_1", is_error=True)
    docent_tool = to_docent_message(tool)
    assert isinstance(docent_tool, DocentToolMessage)
    assert docent_tool.error == {"is_error": True}


def test_to_docent_message_preserves_cacheable_text_and_reasoning_variants() -> None:
    message = AssistantMessage(
        content=(
            CacheableContentText(text="answer", cache_control=CacheControl(ttl="5m")),
            ContentReasoning(text="reasoning"),
            ContentSummarizedReasoning(text="summary", signature="signature"),
            ContentRedactedReasoning(data="encrypted"),
        )
    )
    converted = to_docent_message(message)
    assert isinstance(converted, DocentAssistantMessage)
    assert isinstance(converted.content, list)
    assert [block.model_dump(exclude_none=True) for block in converted.content] == [
        {"type": "text", "text": "answer"},
        {"type": "reasoning", "reasoning": "reasoning", "redacted": False},
        {
            "type": "reasoning",
            "reasoning": "",
            "summary": "summary",
            "signature": "signature",
            "redacted": False,
        },
        {"type": "reasoning", "reasoning": "", "redacted": True},
    ]


@pytest.mark.anyio
async def test_stored_tree_converts_with_group_hierarchy_and_metadata() -> None:
    leaf = Transcript(
        name="leaf",
        description="nested conversation",
        messages=(
            UserMessage(content=(CacheableContentText(text="question", cache_control=None),)),
        ),
        metadata={"scores": [1, 0.5, None, True]},
    )
    inner = TranscriptGroup(
        name="inner", description=None, transcripts=(leaf,), groups=(), metadata={"level": 2}
    )
    outer = TranscriptGroup(
        name="outer", description=None, transcripts=(), groups=(inner,), metadata={"level": 1}
    )
    run = AgentRun(
        name="run",
        description="stored tree",
        transcripts=(),
        groups=(outer,),
        metadata={"ok": True},
    )
    store = MemoryStorage()
    restored = await store.load(await store.store(run))
    converted = to_docent_agent_run(restored)

    assert converted.name == "run"
    assert converted.description == "stored tree"
    assert converted.metadata == {"ok": True}
    assert converted.transcript_groups is not None
    groups = {group.name: group for group in converted.transcript_groups}
    assert groups["outer"].parent_transcript_group_id is None
    assert groups["inner"].parent_transcript_group_id == groups["outer"].id
    assert all(group.agent_run_id == converted.id for group in groups.values())
    assert groups["inner"].metadata == {"level": 2}
    assert len(converted.transcripts) == 1
    transcript = converted.transcripts[0]
    assert transcript.transcript_group_id == groups["inner"].id
    assert transcript.name == "leaf"
    assert transcript.metadata == {"scores": [1, 0.5, None, True]}
    assert transcript.messages[0].content == "question"
