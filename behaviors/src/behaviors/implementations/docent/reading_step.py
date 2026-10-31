"""Replays one Docent ``ReadingStep`` LLM call against a transcript or agent run.

A Docent "reader" runs an LLM call per agent run / transcript / row by:

  1. Rendering a prompt template that interleaves literal text with
     placeholders for context items (transcripts, agent runs, etc).
  2. Sending the rendered prompt to an LLM with a system message that
     describes citation conventions and (optionally) a JSON output schema.
  3. Parsing the response as JSON, validating against the schema, and
     resolving citation markup against the loaded context.

``DocentReadingStep`` replays steps 1–3 against a single ``Transcript`` or
``AgentRun``. It is deliberately not a judge — it's a building block that
mirrors one specific kind of Docent plan step (the LLM-call variant of
``ReadingStep``). The Docent reading config (``prompt_template_segments``,
``context_configs``, ``output_schema``) is passed through verbatim; the
config's ``model`` and ``max_new_tokens`` are ignored — those come from the
injected ``ModelAPI`` and ``SamplingParams``.

Limitations (enforced when the config is constructed):
  - A single bound input (the ``Transcript`` or ``AgentRun`` passed to
    ``read``); its placeholder must be ``transcript``-, ``agent_run``-, or
    ``unknown``-typed.
  - Other prompt-template parameters must either be ``text``-typed with
    values supplied via ``extra_text_params``, or ``reading_result``-typed
    with parsed-response dicts supplied via ``extra_reading_results``.
  - Templates referencing ``transcript_slice`` parameters, or more than one
    transcript/agent-run parameter, raise.
"""

import dataclasses
import json
import logging
from collections.abc import Collection
from typing import Any, cast

import jsonschema
from docent._llm_util.data_models.exceptions import ValidationFailedException
from docent.data_models import AgentRun as DocentAgentRun
from docent.data_models import Transcript as DocentTranscript
from docent.data_models.agent_run import AgentRunTree
from docent.data_models.context_config import (
    ParameterContextConfig,
    validate_parameter_context_config,
)
from docent.judges.util.parse_output import parse_and_validate_output_str
from docent.sdk.llm_context import AgentRunRef, LLMContext, LoadedReadingResult, TranscriptRef

from behaviors.implementations.docent._reading_step_internals import (
    build_spec_for_row,
    is_default_output_schema,
    make_schema_system_message,
    process_citation_fields,
    schema_wants_citations,
)
from behaviors.implementations.docent.conversions import to_docent_agent_run, to_docent_transcript
from behaviors.implementations.models.retries import (
    DEFAULT_FAILURE_CATEGORIES_TO_RETRY,
    generate_with_retries,
)
from behaviors.interfaces.model_apis import ModelAPI
from behaviors.types.agent_run import AgentRun, Transcript
from behaviors.types.content import ContentText
from behaviors.types.failure import FailureCategory, ModelCallFailure
from behaviors.types.json import JSONObject, JSONValue
from behaviors.types.message import AssistantMessage, SystemMessage, UserMessage
from behaviors.types.response import ModelRequest, ModelResponse, SamplingParams
from behaviors.types.retry import RetryConfig

logger = logging.getLogger(__name__)

_INPUT_PARAM_TYPES = frozenset({"transcript", "agent_run", "unknown"})
_SUPPORTED_PARAM_TYPES = frozenset({"transcript", "agent_run", "text", "reading_result", "unknown"})

# Synthetic collection id used for in-memory reading results. The Docent LLM
# context never resolves these against a real store — it just looks them up in
# the ``reading_results`` dict by id — so the value is opaque. We keep it
# constant so the value stays stable across runs and tests.
_LOCAL_READING_RESULT_COLLECTION_ID = "local"

# Output problems worth resampling: the model produced something, but it
# doesn't parse, validate, or citation-resolve.
_OUTPUT_VALIDATION_ERRORS = (
    ValidationFailedException,
    jsonschema.ValidationError,
    json.JSONDecodeError,
    ValueError,
    KeyError,
    IndexError,
    AssertionError,
    TypeError,
)


def _local_reading_result_id(param_name: str) -> str:
    """Stable synthetic reading-result id for a local extra_reading_results entry.

    The id is purely a key for the ``LLMContext.from_spec(reading_results=...)``
    dict; it must match the value we put in the ``arguments`` dict so
    ``build_spec_for_row`` registers a ``ReadingResultRef`` with the same id.
    """
    return f"local-reading-result:{param_name}"


def _iter_param_placeholders(segments: list[Any]) -> list[dict[str, Any]]:
    """Return the param-placeholder segments from a segment list (skips literal strings)."""
    out: list[dict[str, Any]] = []
    for seg in segments:
        if isinstance(seg, dict) and "param_name" in seg:
            out.append(cast(dict[str, Any], seg))
    return out


def _validate_segments(
    segments: list[Any],
    input_param_name: str,
    extra_text_params: dict[str, Any] | None = None,
    extra_reading_results: dict[str, Any] | None = None,
) -> None:
    """Raise ValueError if any placeholder is unsupported or unbound.

    A param is supported iff:
      - it is the input binding (name matches ``input_param_name``, type is
        ``transcript``, ``agent_run``, or ``unknown``), or
      - it is a text param (type ``text`` or ``unknown``) and its name is
        present in ``extra_text_params``, or
      - it is a reading_result param and its name is present in
        ``extra_reading_results``.
    """
    text_extra = extra_text_params or {}
    reading_result_extra = extra_reading_results or {}
    seen_input = False
    for seg in _iter_param_placeholders(segments):
        name = seg["param_name"]
        ptype: str = seg.get("param_type", "unknown")
        is_list = bool(seg.get("is_list", False))

        if ptype not in _SUPPORTED_PARAM_TYPES:
            raise ValueError(
                f"DocentReadingStep: unsupported param_type {ptype!r} for {name!r}; only "
                "'transcript', 'agent_run', 'text', and 'reading_result' are supported."
            )

        if name == input_param_name:
            if is_list:
                raise ValueError(f"DocentReadingStep: input binding {name!r} must not be a list.")
            if ptype not in _INPUT_PARAM_TYPES:
                raise ValueError(
                    f"DocentReadingStep: param {name!r} is bound as the input but declared "
                    f"type {ptype!r} in the template."
                )
            seen_input = True
            continue

        if ptype in ("transcript", "agent_run"):
            raise ValueError(
                f"DocentReadingStep: template references additional {ptype} param {name!r}; "
                f"only one transcript/agent-run binding ({input_param_name!r}) is supported."
            )
        if ptype == "reading_result":
            if name not in reading_result_extra:
                raise ValueError(
                    f"DocentReadingStep: reading_result param {name!r} has no binding; pass "
                    "it in extra_reading_results."
                )
            if is_list:
                raise ValueError(
                    f"DocentReadingStep: reading_result param {name!r} declared is_list=True; "
                    "list-valued reading_result params are not currently supported."
                )
            continue

        # Remaining case: text param (declared 'text' or 'unknown').
        if name in reading_result_extra and name not in text_extra:
            # Allow 'unknown' params bound through extra_reading_results.
            if is_list:
                raise ValueError(
                    f"DocentReadingStep: reading_result param {name!r} declared is_list=True; "
                    "list-valued reading_result params are not currently supported."
                )
            continue
        if name not in text_extra:
            raise ValueError(
                f"DocentReadingStep: param {name!r} has no binding; either set "
                f"input_param_name={name!r}, pass it in extra_text_params, or pass it in "
                "extra_reading_results."
            )
        if is_list and not isinstance(text_extra[name], list):
            raise ValueError(
                f"DocentReadingStep: param {name!r} declared is_list=True but "
                "extra_text_params value is not a list."
            )

    if not seen_input:
        raise ValueError(
            f"DocentReadingStep: prompt template has no placeholder named "
            f"{input_param_name!r} for the input transcript or agent run."
        )


def _declared_input_type(segments: list[Any], input_param_name: str) -> str:
    for seg in _iter_param_placeholders(segments):
        if seg["param_name"] == input_param_name:
            return cast(str, seg.get("param_type", "unknown"))
    raise ValueError(f"no placeholder named {input_param_name!r}")  # unreachable post-validation


def _render_prompt_content_blocks(
    context: LLMContext,
    rendered_segments: list[str | dict[str, str]],
    input_alias: str,
) -> tuple[ContentText, ...]:
    """Render a prompt with the input-item prefix in its own content block.

    ``LLMContext.render_segments`` owns Docent's separator and alias-rendering
    semantics. Rendering once through the input segment and once in full
    preserves those semantics without reproducing them here, while exposing a
    block boundary that model implementations can use as a cache breakpoint.
    """
    input_segment_index = next(
        index
        for index, segment in enumerate(rendered_segments)
        if isinstance(segment, dict) and segment.get("alias") == input_alias
    )
    prefix = context.render_segments(rendered_segments[: input_segment_index + 1])
    prompt = context.render_segments(rendered_segments)
    if not prompt.startswith(prefix):
        raise ValueError("Input-prefix rendering does not match the full Docent prompt")
    suffix = prompt[len(prefix) :]
    if not suffix:
        return (ContentText(text=prefix),)
    return (ContentText(text=prefix), ContentText(text=suffix))


@dataclasses.dataclass(frozen=True, kw_only=True)
class DocentReadingStepResult:
    """A result from running one Docent reading step.

    Attributes:
        parsed_response: The parsed (and citation-resolved) JSON response.
        reader_transcript: System + user prompt + model response, for logging;
            None when the result is the configured failure default.
        retry_error_transcripts: Transcripts of attempts whose output failed
            to parse or validate.
    """

    parsed_response: JSONObject
    reader_transcript: Transcript | None
    retry_error_transcripts: tuple[Transcript, ...]


@dataclasses.dataclass(frozen=True, kw_only=True)
class DocentReadingStepConfig:
    """Configuration for :class:`DocentReadingStep`.

    Attributes:
        prompt_template_segments: Segments from a Docent ``ReadingConfig``
            (strings interleaved with placeholder dicts), passed through
            verbatim.
        output_schema: JSON schema the model output must conform to.
        input_param_name: Which placeholder the input transcript or agent run
            binds to.
        context_configs: ``dict[str, ParameterContextConfig]`` in JSON-dict
            form, or None.
        extra_text_params: Values for other ``text``-typed placeholders
            (``str`` or ``list[str]`` per name), or None.
        extra_reading_results: Parsed-response JSON dicts keyed by
            ``reading_result``-typed placeholder name. Each placeholder
            renders identically to what Docent produces for a real
            ``reading_result`` parameter, so local multi-step flows produce
            the same prompt text as a Docent multi-step reading preset would.
        system_prefix_override: When set, replaces Docent's stock "You are
            tasked with analyzing transcripts..." prefix in the system
            message. Citation guidance and JSON-schema guidance are still
            appended automatically when the schema demands them.
        max_output_attempts: How many times to sample the model when its
            output fails to parse or validate.
        default_parsed_response_on_failure: If set, returned (with a warning)
            when output attempts are exhausted; otherwise the last validation
            error is reraised.
    """

    prompt_template_segments: tuple[JSONValue, ...]
    output_schema: JSONObject
    input_param_name: str
    context_configs: JSONObject | None = None
    extra_text_params: JSONObject | None = None
    extra_reading_results: JSONObject | None = None
    system_prefix_override: str | None = None
    max_output_attempts: int = 3
    default_parsed_response_on_failure: JSONObject | None = None

    def __post_init__(self) -> None:
        _validate_segments(
            segments=list(self.prompt_template_segments),
            input_param_name=self.input_param_name,
            extra_text_params=(
                cast(dict[str, Any], self.extra_text_params)
                if self.extra_text_params is not None
                else None
            ),
            extra_reading_results=(
                cast(dict[str, Any], self.extra_reading_results)
                if self.extra_reading_results is not None
                else None
            ),
        )


class DocentReadingStep:
    """Replays one row of a Docent ``ReadingStep`` against a transcript or agent run.

    The model call goes through the given ``ModelAPI`` with the given
    sampling params; transport failures in ``failure_categories_to_retry``
    are retried per ``retry`` and an exhausted transport failure is returned
    as data. Output that fails to parse or validate is resampled up to
    ``config.max_output_attempts`` times.
    """

    def __init__(
        self,
        *,
        config: DocentReadingStepConfig,
        model_api: ModelAPI,
        sampling: SamplingParams,
        retry: RetryConfig | None = None,
        failure_categories_to_retry: Collection[
            FailureCategory
        ] = DEFAULT_FAILURE_CATEGORIES_TO_RETRY,
    ):
        self.config = config
        self._model_api = model_api
        self._sampling = sampling
        self._retry = retry
        self._failure_categories_to_retry = failure_categories_to_retry

    def _build_system_message(
        self,
        context: LLMContext,
        wants_citations: bool,
        output_schema: dict[str, Any],
        is_default_output: bool,
    ) -> str:
        """Build the system prompt, mirroring Docent's reading_worker.

        Docent emits ``<prefix>\\n\\n<citation block>`` (citation block omitted
        when not requested). When ``system_prefix_override`` is set, we ask the
        SDK for the canonical prefix and swap it for our own; this keeps the
        citation block in lockstep with the SDK without copying the string.
        """
        if self.config.system_prefix_override is None:
            system = context.get_system_message(
                interactive=False, include_citations=wants_citations
            )
        else:
            canonical_prefix = context.get_system_message(
                interactive=False, include_citations=False
            )
            full = context.get_system_message(interactive=False, include_citations=wants_citations)
            system = full.replace(canonical_prefix, self.config.system_prefix_override, 1)

        if not is_default_output:
            system = f"{system}\n\n{make_schema_system_message(output_schema)}"
        return system

    async def read(
        self, source: Transcript | AgentRun
    ) -> DocentReadingStepResult | ModelCallFailure:
        """Run the reading step against one transcript or agent run.

        Returns the parsed result, or the last transport ``ModelCallFailure``
        once retries are exhausted. Raises the last validation error when the
        model's output never parses and no failure default is configured.
        """
        segments_list = list(self.config.prompt_template_segments)
        declared = _declared_input_type(segments_list, self.config.input_param_name)

        docent_transcripts: dict[str, DocentTranscript] = {}
        docent_agent_runs: dict[str, DocentAgentRun] = {}
        if isinstance(source, Transcript):
            if declared == "agent_run":
                raise ValueError(
                    "DocentReadingStep: the template binds an agent_run parameter but read() "
                    "was given a Transcript."
                )
            docent_transcript = to_docent_transcript(source)
            docent_transcripts[docent_transcript.id] = docent_transcript
            input_ref: dict[str, Any] = {
                "id": docent_transcript.id,
                "agent_run_id": "",
                "collection_id": "",
                "type": "transcript",
            }
        else:
            if declared == "transcript":
                raise ValueError(
                    "DocentReadingStep: the template binds a transcript parameter but read() "
                    "was given an AgentRun."
                )
            docent_run = to_docent_agent_run(source)
            docent_agent_runs[docent_run.id] = docent_run
            input_ref = {
                "id": docent_run.id,
                "collection_id": "",
                "type": "agent_run",
            }

        output_schema = cast(dict[str, Any], self.config.output_schema)

        context_configs_dict: dict[str, ParameterContextConfig] | None = None
        if self.config.context_configs is not None:
            context_configs_dict = {
                k: validate_parameter_context_config(v)
                for k, v in self.config.context_configs.items()
            }

        extra_text = cast(dict[str, Any], self.config.extra_text_params or {})
        extra_reading_results = cast(dict[str, Any], self.config.extra_reading_results or {})

        reading_result_args: dict[str, Any] = {}
        loaded_reading_results: dict[str, LoadedReadingResult] = {}
        for param_name, parsed_output in extra_reading_results.items():
            local_id = _local_reading_result_id(param_name)
            reading_result_args[param_name] = {
                "id": local_id,
                "collection_id": _LOCAL_READING_RESULT_COLLECTION_ID,
                "type": "reading_result",
            }
            loaded_reading_results[local_id] = LoadedReadingResult(
                id=local_id,
                collection_id=_LOCAL_READING_RESULT_COLLECTION_ID,
                output=cast(dict[str, Any], parsed_output),
            )

        arguments: dict[str, Any] = {
            self.config.input_param_name: input_ref,
            **extra_text,
            **reading_result_args,
        }

        spec, rendered_segments = build_spec_for_row(
            prompt_template_segments=segments_list,
            arguments=arguments,
            context_configs=context_configs_dict,
        )

        # Rendering an agent run resolves its transcripts through spec-level
        # transcript items, so register them the way the SDK's
        # ``add_agent_run_from_object`` does: one non-root transcript per run
        # transcript, in the run tree's order.
        for docent_run in docent_agent_runs.values():
            tree = AgentRunTree.from_agent_run(docent_run)
            for t_id in sorted(
                tree.transcript_id_to_idx, key=lambda t_id: tree.transcript_id_to_idx[t_id]
            ):
                spec.add_transcript(
                    id=t_id, agent_run_id=docent_run.id, collection_id="", is_root=False
                )
                docent_transcripts[t_id] = docent_run.transcript_dict[t_id]

        context = LLMContext.from_spec(
            spec,
            agent_runs=docent_agent_runs,
            transcripts=docent_transcripts,
            reading_results=loaded_reading_results,
        )

        input_alias = next(
            alias
            for alias, ref in context.spec.items.items()
            if (isinstance(ref, TranscriptRef) or isinstance(ref, AgentRunRef))
            and ref.id == input_ref["id"]
        )

        wants_citations = schema_wants_citations(output_schema)
        is_default_output = is_default_output_schema(output_schema)

        prompt_content = _render_prompt_content_blocks(context, rendered_segments, input_alias)
        system_text = self._build_system_message(
            context=context,
            wants_citations=wants_citations,
            output_schema=output_schema,
            is_default_output=is_default_output,
        )

        system_message = SystemMessage(content=(ContentText(text=system_text),))
        user_message = UserMessage(content=prompt_content)
        request = ModelRequest(
            context=(system_message, user_message),
            tools=(),
            tool_choice=None,
            parallel_tool_calls=None,
            sampling=self._sampling,
        )

        retry_error_transcripts: list[Transcript] = []
        last_error: Exception | None = None
        for _ in range(self.config.max_output_attempts):
            response = await generate_with_retries(
                self._model_api,
                request,
                retry=self._retry,
                failure_categories_to_retry=self._failure_categories_to_retry,
            )
            if isinstance(response, ModelCallFailure):
                return response

            try:
                content_text = _response_text(response)
                if is_default_output:
                    parsed: dict[str, Any] = {"output": content_text}
                else:
                    parsed = parse_and_validate_output_str(content_text, output_schema)
                if wants_citations:
                    parsed = cast(
                        dict[str, Any], process_citation_fields(parsed, output_schema, context)
                    )
            except _OUTPUT_VALIDATION_ERRORS as error:
                last_error = error
                retry_error_transcripts.append(
                    Transcript(
                        name=None,
                        description=f"Retry error: {type(error).__name__}",
                        messages=(system_message, user_message, *response.output),
                        metadata={
                            "is_retry_error": True,
                            "error_type": type(error).__name__,
                            "error_message": str(error),
                        },
                    )
                )
                continue

            reader_transcript = Transcript(
                name=None,
                description=None,
                messages=(system_message, user_message, *response.output),
                metadata={"reader_parsed_response": cast(JSONValue, parsed)},
            )
            return DocentReadingStepResult(
                parsed_response=parsed,
                reader_transcript=reader_transcript,
                retry_error_transcripts=tuple(retry_error_transcripts),
            )

        if self.config.default_parsed_response_on_failure is not None:
            logger.warning(
                "DocentReadingStep output never validated after %d attempts; returning the "
                "configured default",
                self.config.max_output_attempts,
            )
            return DocentReadingStepResult(
                parsed_response=self.config.default_parsed_response_on_failure,
                reader_transcript=None,
                retry_error_transcripts=tuple(retry_error_transcripts),
            )
        assert last_error is not None
        raise last_error


def _response_text(response: ModelResponse) -> str:
    """The first text block of the response's first assistant message.

    Raises ValueError when the response has no assistant text, which counts
    as an invalid output attempt.
    """
    for message in response.output:
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ContentText):
                    return block.text
    raise ValueError("model response contains no assistant text")
