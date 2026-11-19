"""``judge`` asks a model to assess some entities and returns a ``Judgment``.

Also here:

- ``CustomFormat``: a field of the answer that needs a check of its own, such as text with
  citations.
- ``fill_context_template``: fills in a template like the one ``judge`` takes, for use outside
  ``judge``.

``jsonschema`` does the schema checking. ``behaviors`` installs it only with the ``docent``
extra.
"""

import dataclasses
import hashlib
import json
import math
import re
import typing
import urllib.parse
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from typing import Any, cast

import jsonschema
from jsonschema.exceptions import best_match
from jsonschema.validators import extend as extend_validator

from behaviors.implementations.models.retries import (
    DEFAULT_FAILURE_CATEGORIES_TO_RETRY,
    generate_with_retries,
)
from behaviors.interfaces.model_apis import ModelAPI, ModelCallFailedError
from behaviors.judging.citations import (
    Citation,
    citation_instructions_text,
    find_citations_in_output,
)
from behaviors.types.content import ContentText
from behaviors.types.failure import FailureCategory, ModelCallFailure
from behaviors.types.json import JSONObject, JSONValue
from behaviors.types.message import AssistantMessage, ChatMessage, UserMessage
from behaviors.types.namespace import behaviors_type
from behaviors.types.response import (
    ModelRequest,
    ModelResponse,
    SamplingParams,
    StopMaxTokens,
)
from behaviors.types.retry import RetryConfig
from behaviors.types.streaming import StreamEventCallback, StreamRestart
from fxtr.core.entities import Entity
from fxtr.core.entity_data import EntityID
from fxtr.entity_defns.dataclass_bases import TaggedDataclass
from fxtr.entity_defns.dataclass_entity import DataclassEntity

type EntityToPromptText = Callable[[Entity], str]
"""A function that writes one entity as text for the judge model to read.

It takes the entity and returns the text. ``judge`` puts that text in its request.
"""

type CustomFormatValidator = Callable[[Mapping[str, Entity], JSONValue], str | None]
"""A function that checks the value of one field of the model's answer.

It takes two arguments: the entities given to ``judge``, under their names, and the field's
value.

It returns None when the value is valid. Otherwise it returns a message saying what is wrong.
``judge`` sends that message to the model so it can correct its answer.

``judge`` only calls it with a value that fits the rest of the field's schema. For example, a
validator for a field declared ``"type": "string"`` always receives a string.
"""


@dataclasses.dataclass(frozen=True)
class CustomFormat:
    """Use a ``CustomFormat`` when a field of the judge's answer needs a check that JSON Schema
    cannot express. An example is text whose citations must name lines that exist.

    Define it once and use the same object twice:

    - Call its ``schema`` method to write the field into the output schema.
    - Pass it to ``judge`` in ``custom_formats``, so ``judge`` runs the check.

    For example::

        discussion = fmt.schema(description="Why, citing the evidence.")
        output_schema = binary_schema(match_description="...", discussion=discussion)
        judgment = await judge(..., output_schema=output_schema, custom_formats=[fmt])

    Some checks depend on how an entity is shown to the model. A citation check, for example,
    depends on the line labels. For those, the format also holds the function that writes the
    entity as text, and the instructions that explain that text. Keeping all three in one object
    means they cannot disagree.

    A check that only looks at the value, such as a list with no repeats, leaves both mappings
    empty.

    Attributes:
        name: The ``format`` that a field of this kind has in the output schema, such as
            ``"citations"``. ``judge`` uses it to find the format for a field.
        value_schema: The JSON Schema of the field's value, without the format. For example,
            ``{"type": "string"}``. The validator is only called with a value that fits it.
        validator: The function that checks the value of each field with this format.
        entity_to_prompt_text: A mapping from entity class to the function that writes that
            class as prompt text. Fill it in when the validator depends on how an entity is
            written. A citation format does, because its validator checks citations against the
            line labels. ``judge`` writes entities of these classes with these functions. Leave
            it empty otherwise.
        entity_instructions: A mapping from entity class to the text that explains to the model
            how that class is written. For a citation format, the text says what the line labels
            are and how to cite them. Every class here must also be in
            ``entity_to_prompt_text``. ``judge`` puts the text where the template has
            ``{citation_instructions}``, for each class it was given an entity of. Leave it
            empty if there is nothing to explain.
    """

    name: str
    value_schema: JSONObject
    validator: CustomFormatValidator
    entity_to_prompt_text: Mapping[type[Entity], EntityToPromptText]
    entity_instructions: Mapping[type[Entity], str]

    def schema(self, *, description: str) -> JSONObject:
        """The schema for one field of this format, to use inside an output schema.

        Args:
            description: What to write in the field, as the model reads it.
        """
        return {**self.value_schema, "description": description, "format": self.name}

    def __post_init__(self) -> None:
        unwritten = sorted(
            entity_class.__name__
            for entity_class in self.entity_instructions.keys() - self.entity_to_prompt_text.keys()
        )
        if unwritten:
            raise ValueError(
                f"entity_instructions describes {', '.join(unwritten)}, which"
                " entity_to_prompt_text has no entry for, so the text would describe"
                " prompt text this format does not write."
            )


@dataclasses.dataclass(frozen=True)
class InvalidOutputReason(TaggedDataclass, fxtr_type=behaviors_type("InvalidOutputReason.v1")):
    """One reason ``judge`` did not accept an answer.

    Attributes:
        path_in_output: Where in the output the problem is. It is the keys that lead there from
            the top of the answer. An int is a position in a list. For a missing field, it is
            the path of the object that lacks the field. It is empty when the problem is with
            the answer as a whole, such as an answer that was cut off.
        description: What is wrong there. This is the text the model is sent so it can correct
            its answer.
    """

    path_in_output: tuple[str | int, ...]
    description: str


@dataclasses.dataclass(frozen=True)
class JudgeAttempt(TaggedDataclass, fxtr_type=behaviors_type("JudgeAttempt.v1")):
    """One request ``judge`` sent to the model, and what came of it.

    An attempt is one of three cases:

    - The call failed. ``response`` is a ``ModelCallFailure`` and ``invalid_output_reasons`` is
      empty. It is the last attempt of its judgment. A failed call that was made again and then
      succeeded is not an attempt.
    - The answer was not accepted. ``response`` is a ``ModelResponse`` and
      ``invalid_output_reasons`` says why.
    - The answer was accepted. ``response`` is a ``ModelResponse`` and
      ``invalid_output_reasons`` is empty.

    Attributes:
        request: The request that was sent.
        response: The model's response, or the failure of the call.
        invalid_output_reasons: Each reason the model's answer was not accepted. A reason can be
            that the answer is not one JSON object, was cut off, does not fit the output schema,
            failed a custom format's validator, or cannot
            be stored by fxtr.
    """

    request: ModelRequest
    response: ModelResponse | ModelCallFailure
    invalid_output_reasons: tuple[InvalidOutputReason, ...]


@dataclasses.dataclass(frozen=True)
class Judgment(DataclassEntity, fxtr_type=behaviors_type("Judgment.v1")):
    """The result of one ``judge`` call.

    Store the entities before the judgment. A judgment refers to the entities it was given by
    ID, and fxtr only stores a record whose references it already holds. In a step, an entity
    the step received as an input is already stored. An entity the step built itself, such as a
    rubric written inline, must be stored first with ``context.store``.

    Attributes:
        entity_ids: The ID of each entity given to ``judge``, under the name it was given.
        output: The model's answer as it wrote it, with the citation markers still in the text.
            None if no answer was accepted.
        citations: One entry for each citation in a field of ``output`` that has a custom
            format. A field with no custom format is free text, and is not searched for
            citations. Empty when ``output`` is None.
        attempts: Every answer the model gave, in order. If a failed call ended the judgment,
            that call comes last. When ``output`` is None, the last attempt says why.
    """

    entity_ids: Mapping[str, EntityID]
    output: JSONObject | None
    citations: tuple[Citation, ...]
    # TODO: Store less here. Each attempt holds its whole request, and each request repeats the
    # entities as prompt text, so a judgment with three attempts holds the evidence three times, and
    # anything that loads a judgment for its `output` loads all of it. Storing the first request
    # once (later ones are it plus the earlier answers and correction messages), and moving the
    # attempts into an entity of their own that the judgment refers to by ID, would fix both.
    attempts: tuple[JudgeAttempt, ...]


# The failures that end in a returned ``Judgment`` whose ``output`` is None, because every run
# would fail the same way. Any other failure raises ``ModelCallFailedError``. This is not an
# argument of ``judge``: a failure put here by mistake, such as a rate limit, would be cached as
# the step's result and never tried again.
_FAILURE_CATEGORIES_TO_RECORD: tuple[FailureCategory, ...] = ("context_length",)

# Every value of ``FailureCategory``, to check ``failure_categories_to_retry`` against.
_FAILURE_CATEGORIES: frozenset[str] = frozenset(typing.get_args(FailureCategory.__value__))


async def judge(
    model_api: ModelAPI,
    *,
    context_template: Sequence[ChatMessage],
    entities: Mapping[str, Entity],
    entity_to_prompt_text: Mapping[type[Entity], EntityToPromptText],
    output_schema: JSONObject,
    sampling: SamplingParams,
    callback: StreamEventCallback | None = None,
    custom_formats: Sequence[CustomFormat] = (),
    max_answers: int = 3,
    retry: RetryConfig = RetryConfig(),
    failure_categories_to_retry: Collection[FailureCategory] = DEFAULT_FAILURE_CATEGORIES_TO_RETRY,
) -> Judgment:
    """Ask ``model_api`` for a judgment of ``entities``.

    ``judge`` fills in ``context_template``, calls the model, and checks the answer against
    ``output_schema``. If the answer is not accepted, ``judge`` sends it back to the model with
    what was wrong, in the same conversation.

    Args:
        model_api: The model to ask.
        context_template: The messages to send. ``judge`` replaces three kinds of placeholder in
            their text:

            - ``{<name>}``: the entity of that name, as text.
            - ``{output_schema}``: the output schema. The template must have this one.
            - ``{citation_instructions}``: how to write a citation, followed by each custom
              format's instructions for the entities it writes. It is replaced with nothing when
              no custom format is used.

            A placeholder is a single word in braces, so a template can contain JSON. Only the
            template is searched, so braces inside an entity's own text are sent as they are.
        entities: Everything the model is given, each under a name. This includes the
            instructions, the rubric and what is being assessed. The name is the entity's
            placeholder in the template. It is also what the model cites the entity by. A name
            is made of letters, digits and underscores.
        entity_to_prompt_text: A mapping from entity class to the function that writes that
            class as prompt text. It needs an entry for the class of each entity that no custom
            format writes. The lookup is by exact class.
        output_schema: The JSON Schema for the model's answer. It must describe an object.
        sampling: The sampling parameters for each call.
        callback: Receives the events of each call if ``model_api`` can stream. A
            ``StreamRestart`` is sent before every call after the first.
        custom_formats: Every ``CustomFormat`` that ``output_schema`` has a field of.
        max_answers: How many answers the model gets in total. Each answer that is not accepted
            goes back to the model with what was wrong, until it has given this many. Must be a
            whole number of at least 1. Each attempt is stored with its whole request, which
            grows with every correction, so a large number costs storage as well as calls.
        retry: How often to make a call again when it fails with one of
            ``failure_categories_to_retry``, and how long to wait in between. A failed call that
            is made again does not use up one of the model's answers. If ``model_api`` already
            retries failed calls, the two multiply. Pass ``RetryConfig(max_attempts=1)`` to make
            each call once.
        failure_categories_to_retry: The failures that ``retry`` applies to, from the values of
            ``FailureCategory``. The default is the failures that may pass on their own: a rate
            limit, an overloaded provider and a server error.

    Returns:
        The judgment. Its ``output`` is None in two cases: no answer was accepted, or the
        request is too long for the model's context. These are returned, not raised, because
        every run would end the same way. A step that returns has its result cached.

    Raises:
        ModelCallFailedError: A call failed for any other reason, such as a rate limit, an
            outage or a wrong API key. This is raised because a later run may succeed. A step
            that raises runs again when the job is resumed.
        ValueError: ``judge`` was called wrongly. This is raised before the model is called. The
            causes are:

            - The template's placeholders and ``entities`` do not match up.
            - ``output_schema`` cannot be used.
            - ``custom_formats`` does not match the formats the schema names, or two formats
              write the same class differently.
            - An entity's class has no entry in ``entity_to_prompt_text``.
            - ``max_answers`` is not a whole number of at least 1.
            - ``failure_categories_to_retry`` has a value that is not a ``FailureCategory``.
    """
    if isinstance(max_answers, bool) or not isinstance(max_answers, int) or max_answers < 1:
        raise ValueError(f"max_answers must be a whole number of at least 1, not {max_answers!r}.")
    # Read once, so a generator is not used up, and checked, so a typo does not turn retries off.
    categories_to_retry = frozenset(failure_categories_to_retry)
    unknown = sorted(categories_to_retry - _FAILURE_CATEGORIES)
    if unknown:
        raise ValueError(
            f"failure_categories_to_retry has {', '.join(map(repr, unknown))}, which is not a"
            f" failure category. The categories are: {', '.join(sorted(_FAILURE_CATEGORIES))}."
        )
    formats = _custom_formats_by_name(custom_formats)
    validators = {name: custom_format.validator for name, custom_format in formats.items()}
    _check_output_schema(output_schema, validators)
    to_prompt_text = _to_prompt_text_with_custom_formats(entity_to_prompt_text, formats)
    entity_instructions = _entity_instructions(entities, formats)
    _check_placeholders_and_entity_names(
        context_template, entities, needs_citation_instructions=bool(entity_instructions)
    )
    context = fill_context_template(
        context_template,
        {
            **{
                name: _wrap_in_section(name, _write_as_prompt_text(entity, to_prompt_text))
                for name, entity in entities.items()
            },
            _OUTPUT_SCHEMA_PLACEHOLDER_NAME: _output_schema_in_section(output_schema),
            # With no custom format, no field is searched for citations, so the model is not
            # asked for any.
            _CITATION_INSTRUCTIONS_PLACEHOLDER_NAME: "\n".join(
                (citation_instructions_text(entities.keys()), *entity_instructions)
            )
            if formats
            else "",
        },
    )
    # Computed once, before the first call: an entity fxtr cannot store fails here, and not after
    # the model has been called.
    entity_ids = {name: entity.content_id() for name, entity in entities.items()}
    answer_validator, checked_values = _answer_validator(output_schema, validators, entities)

    attempts: list[JudgeAttempt] = []

    def judgment(output: JSONObject | None, citations: tuple[Citation, ...]) -> Judgment:
        return Judgment(
            entity_ids=entity_ids,
            output=output,
            citations=citations,
            attempts=tuple(attempts),
        )

    while True:
        request = ModelRequest(
            context=context,
            tools=(),
            tool_choice=None,
            parallel_tool_calls=None,
            sampling=sampling,
        )
        response = await generate_with_retries(
            model_api,
            request,
            retry=retry,
            failure_categories_to_retry=categories_to_retry,
            callback=callback,
        )
        if isinstance(response, ModelCallFailure):
            attempts.append(
                JudgeAttempt(request=request, response=response, invalid_output_reasons=())
            )
            if response.category in _FAILURE_CATEGORIES_TO_RECORD:
                return judgment(None, ())
            # Raised, not returned: a step that returned this would have it cached.
            raise ModelCallFailedError(response)
        output, citations, invalid_output_reasons = _read_answer(
            response, answer_validator, checked_values, entity_ids
        )
        attempts.append(
            JudgeAttempt(
                request=request, response=response, invalid_output_reasons=invalid_output_reasons
            )
        )
        if not invalid_output_reasons:
            return judgment(output, citations)
        if len(attempts) >= max_answers:
            return judgment(None, ())
        context = (
            *context,
            *response.output,
            _correction_message(invalid_output_reasons, output_schema),
        )
        if callback is not None:
            await callback(StreamRestart())


# The names of the two placeholders that ``judge`` replaces itself: with the output schema, and
# with the instructions for citing an entity.
_OUTPUT_SCHEMA_PLACEHOLDER_NAME = "output_schema"
_CITATION_INSTRUCTIONS_PLACEHOLDER_NAME = "citation_instructions"

# Matches one placeholder: one word in braces, such as {run}. The word is the placeholder's
# name. Braces around anything else, such as JSON, are not a placeholder.
_PLACEHOLDER_PATTERN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _check_placeholders_and_entity_names(
    context_template: Sequence[ChatMessage],
    entities: Mapping[str, Entity],
    *,
    needs_citation_instructions: bool,
) -> None:
    """Raise ``ValueError`` if the template and ``entities`` do not fit together.

    The causes are:

    - An entity's name cannot be a placeholder, or is a name ``judge`` fills in itself.
    - The template has no ``{output_schema}``.
    - The template has a placeholder that is not an entity's name.
    - An entity has no placeholder in the template.
    - A custom format has instructions, and the template has no ``{citation_instructions}``.
    """
    for name in entities:
        if name in (_OUTPUT_SCHEMA_PLACEHOLDER_NAME, _CITATION_INSTRUCTIONS_PLACEHOLDER_NAME):
            raise ValueError(
                f"An entity cannot be named {name!r}: judge replaces {{{name}}} itself."
            )
        if _PLACEHOLDER_PATTERN.fullmatch(f"{{{name}}}") is None:
            raise ValueError(
                f"The entity name {name!r} cannot be a placeholder. Use letters, digits and"
                " underscores, not starting with a digit."
            )
    placeholder_names = {
        match.group(1)
        for message in context_template
        for content in message.content
        if isinstance(content, ContentText)
        for match in _PLACEHOLDER_PATTERN.finditer(content.text)
    }
    if _OUTPUT_SCHEMA_PLACEHOLDER_NAME not in placeholder_names:
        raise ValueError(
            "context_template has no {output_schema}, so the model would not be told what to"
            " return."
        )
    unknown = sorted(
        placeholder_names
        - entities.keys()
        - {_OUTPUT_SCHEMA_PLACEHOLDER_NAME, _CITATION_INSTRUCTIONS_PLACEHOLDER_NAME}
    )
    if unknown:
        names = ", ".join(f"{{{name}}}" for name in unknown)
        raise ValueError(
            f"context_template has the placeholder(s) {names}, which entities has no entry for."
        )
    unused = sorted(entities.keys() - placeholder_names)
    if unused:
        names = ", ".join(f"{{{name}}}" for name in unused)
        raise ValueError(
            f"context_template has no {names}, so the model would not be given"
            f" the entity or entities of that name."
        )
    if (
        needs_citation_instructions
        and _CITATION_INSTRUCTIONS_PLACEHOLDER_NAME not in placeholder_names
    ):
        raise ValueError(
            "context_template has no {citation_instructions}, so the model would not be told how"
            " the entities are written and cited, which a custom format has instructions for."
        )


def fill_context_template(
    context_template: Sequence[ChatMessage], text_by_placeholder_name: Mapping[str, str]
) -> tuple[ChatMessage, ...]:
    """Fill in a context template like the one ``judge`` takes, with text of your own.

    Use this to build messages from a template outside ``judge``. It follows the same
    placeholder rules as ``judge``.

    It only puts text in. It does not do the other things ``judge`` does to build its request:

    - It does not write entities as prompt text.
    - It does not enclose anything in a named section.
    - It does not add the output schema or the citation instructions.

    So its result is not the request ``judge`` would send.

    The placeholder rules:

    - A placeholder is one word in braces, such as ``{run}``. The word is letters, digits and
      underscores, and does not start with a digit.
    - Braces around anything else, such as JSON, are left as they are.
    - Only the text of the template's messages is searched. Text that was put in is not searched
      again.
    - Content that is not text, such as a tool call, is left as it is.

    Args:
        context_template: The messages, whose text has placeholders.
        text_by_placeholder_name: The text to put in place of each placeholder. The key is the
            placeholder's name without its braces, as in ``{"run": "<the run as prompt
            text>"}``. An entry that the template has no placeholder for is not used.

    Returns:
        The messages with every placeholder replaced.

    Raises:
        ValueError: The template has a placeholder that ``text_by_placeholder_name`` has no
            entry for.
    """
    missing = sorted(
        {
            match.group(1)
            for message in context_template
            for content in message.content
            if isinstance(content, ContentText)
            for match in _PLACEHOLDER_PATTERN.finditer(content.text)
        }
        - text_by_placeholder_name.keys()
    )
    if missing:
        names = ", ".join(f"{{{name}}}" for name in missing)
        raise ValueError(
            f"context_template has the placeholder(s) {names}, which text_by_placeholder_name has"
            " no entry for."
        )

    def fill_content(content: Any) -> Any:
        if not isinstance(content, ContentText):
            return content
        text = _PLACEHOLDER_PATTERN.sub(
            lambda match: text_by_placeholder_name[match.group(1)], content.text
        )
        return dataclasses.replace(content, text=text)

    return tuple(
        dataclasses.replace(
            message, content=tuple(fill_content(content) for content in message.content)
        )
        for message in context_template
    )


def _output_schema_in_section(output_schema: JSONObject) -> str:
    return _wrap_in_section(
        _OUTPUT_SCHEMA_PLACEHOLDER_NAME, json.dumps(output_schema, ensure_ascii=False, indent=2)
    )


def _custom_formats_by_name(custom_formats: Sequence[CustomFormat]) -> dict[str, CustomFormat]:
    """``custom_formats`` under their names. Raises ``ValueError`` if two have the same name."""
    formats: dict[str, CustomFormat] = {}
    for custom_format in custom_formats:
        if custom_format.name in formats:
            raise ValueError(
                f"custom_formats has two formats named {custom_format.name!r}. A field's format"
                " names one custom format: give one of them another name."
            )
        formats[custom_format.name] = custom_format
    return formats


def _to_prompt_text_with_custom_formats(
    entity_to_prompt_text: Mapping[type[Entity], EntityToPromptText],
    custom_formats: Mapping[str, CustomFormat],
) -> dict[type[Entity], EntityToPromptText]:
    """The function that writes each entity class as prompt text.

    It combines the functions in ``entity_to_prompt_text`` with the ones each custom format
    needs.

    An entity is written once, so a class has one function. A ``ValueError`` is raised if a
    custom format needs a different function for a class than ``entity_to_prompt_text`` has, or
    than another custom format has. The same function given in both places is not a conflict.
    """
    to_prompt_text = dict(entity_to_prompt_text)
    given_by = dict.fromkeys(entity_to_prompt_text, "entity_to_prompt_text")
    for format_name, custom_format in custom_formats.items():
        for entity_class, function in custom_format.entity_to_prompt_text.items():
            # ``==``, not ``is``: the same method looked up twice is two objects that are equal.
            if entity_class in to_prompt_text and to_prompt_text[entity_class] != function:
                raise ValueError(
                    f"The custom format {format_name!r} writes {entity_class.__name__} as prompt"
                    f" text with a different function than {given_by[entity_class]} does. An"
                    " entity is written once, and a format's validator relies on its own: leave"
                    f" {entity_class.__name__} out of one of the two."
                )
            to_prompt_text[entity_class] = function
            given_by.setdefault(entity_class, f"the custom format {format_name!r}")
    return to_prompt_text


def _entity_instructions(
    entities: Mapping[str, Entity], custom_formats: Mapping[str, CustomFormat]
) -> list[str]:
    """What the custom formats say about the classes of ``entities``. Each text appears once, in
    the order of the entities.
    """
    texts: list[str] = []
    for entity in entities.values():
        for custom_format in custom_formats.values():
            text = custom_format.entity_instructions.get(type(entity))
            if text is not None and text not in texts:
                texts.append(text)
    return texts


def _write_as_prompt_text(
    entity: Entity, to_prompt_text: Mapping[type[Entity], EntityToPromptText]
) -> str:
    function = to_prompt_text.get(type(entity))
    if function is None:
        raise ValueError(
            f"entity_to_prompt_text has no entry for {type(entity).__name__}. Add one"
            " for that class, or use a custom format that writes it."
        )
    return function(entity)


# Where JSON Schema puts schemas inside a schema: under these keywords as one schema, as a list
# of schemas, or as a mapping from names to schemas.
_SCHEMA_KEYWORDS = (
    "additionalProperties",
    "contains",
    "contentSchema",
    "else",
    "if",
    "items",
    "not",
    "propertyNames",
    "then",
    "unevaluatedItems",
    "unevaluatedProperties",
)
_SCHEMA_LIST_KEYWORDS = ("allOf", "anyOf", "oneOf", "prefixItems")
_SCHEMA_MAP_KEYWORDS = (
    "$defs",
    "definitions",
    "dependentSchemas",
    "patternProperties",
    "properties",
)


def _schemas_in(schema: JSONValue) -> Iterator[JSONObject]:
    """``schema`` and every schema nested in it.

    Only the places where JSON Schema puts schemas are searched. What is under ``const``,
    ``enum``, ``default`` or ``examples`` is data, and a property can have any name, so neither
    is read as a keyword.
    """
    if not isinstance(schema, dict):
        return
    yield schema
    for keyword in _SCHEMA_KEYWORDS:
        yield from _schemas_in(schema.get(keyword))
    for keyword in _SCHEMA_LIST_KEYWORDS:
        listed = schema.get(keyword)
        if isinstance(listed, list):
            for item in listed:
                yield from _schemas_in(item)
    for keyword in _SCHEMA_MAP_KEYWORDS:
        named = schema.get(keyword)
        if isinstance(named, dict):
            for item in named.values():
                yield from _schemas_in(item)


def _check_output_schema(
    output_schema: JSONObject, validators: Mapping[str, CustomFormatValidator]
) -> None:
    """Raise ``ValueError`` if ``judge`` cannot use ``output_schema``."""
    try:
        jsonschema.Draft202012Validator.check_schema(cast(dict[str, Any], output_schema))
    except jsonschema.SchemaError as error:
        raise ValueError(f"output_schema is not a valid JSON Schema: {error.message}") from error
    if output_schema.get("type") != "object":
        raise ValueError(
            'output_schema must describe an object: its "type" must be "object". To ask for a'
            " single value, make it the one property of an object."
        )
    schemas = list(_schemas_in(output_schema))
    # A schema with an "$id" of its own resolves references against itself, not the root.
    can_follow_pointers = not any("$id" in schema for schema in schemas[1:])
    for schema in schemas:
        for keyword in ("$ref", "$dynamicRef"):
            reference = schema.get(keyword)
            if isinstance(reference, str):
                _check_reference(reference, output_schema, follow_pointer=can_follow_pointers)
    named: set[str] = set()
    for schema in schemas:
        format_name = schema.get("format")
        if isinstance(format_name, str):
            named.add(format_name)
    unused = sorted(validators.keys() - named)
    if unused:
        raise ValueError(
            f"custom_formats has the format(s) {', '.join(unused)}, which output_schema never"
            " names, so its validator would never run. Give a field that format, with the"
            " custom format's schema method, or leave the format out."
        )
    missing = sorted(named - validators.keys())
    if missing:
        raise ValueError(
            f"output_schema names the format(s) {', '.join(missing)}, which custom_formats has"
            " no entry for. A field with such a format would go unchecked."
        )


def _check_reference(reference: str, root: JSONObject, *, follow_pointer: bool) -> None:
    """Raise if ``reference`` leaves the schema, or is a JSON pointer to something not in it.

    Only references that start with ``#`` are allowed, so checking an answer never fetches
    anything.
    """
    if not reference.startswith("#"):
        raise ValueError("output_schema may use only local JSON Schema references")
    pointer = reference[1:]
    if not follow_pointer or (pointer and not pointer.startswith("/")):
        return  # A named anchor, which jsonschema resolves.
    target: JSONValue = root
    for escaped in pointer.split("/")[1:]:
        token = urllib.parse.unquote(escaped).replace("~1", "/").replace("~0", "~")
        if isinstance(target, dict) and token in target:
            target = target[token]
        elif isinstance(target, list) and token.isdecimal() and int(token) < len(target):
            target = target[int(token)]
        else:
            raise ValueError(f"output_schema refers to {reference!r}, which it does not define")


def _answer_validator(
    output_schema: JSONObject,
    validators: Mapping[str, CustomFormatValidator],
    entities: Mapping[str, Entity],
) -> tuple[Any, list[JSONValue]]:
    """Build the JSON Schema validator that checks answers.

    Returns two things:

    - The validator. It checks a field's ``format`` with the validator of the custom format of
      that name.
    - A list. Each time a custom format's validator is called, the value it was called with is
      added to this list.

    A custom format's validator is only called with a value that fits the rest of its field's
    schema. Otherwise a field of the wrong type would reach a validator written for the right
    type, and the exception would end the judgment.
    """

    checked_values: list[JSONValue] = []

    def check_format(
        validator: Any, format_name: str, value: JSONValue, schema: JSONObject
    ) -> Iterator[jsonschema.ValidationError]:
        check = validators.get(format_name)
        if check is None:
            return
        rest_of_schema = {keyword: item for keyword, item in schema.items() if keyword != "format"}
        if next(iter(validator.descend(value, rest_of_schema)), None) is not None:
            return
        checked_values.append(value)
        problem = check(entities, value)
        if problem is not None:
            yield jsonschema.ValidationError(problem)

    validator_class = extend_validator(  # pyright: ignore[reportUnknownVariableType]
        jsonschema.Draft202012Validator, {"format": check_format}
    )
    return validator_class(output_schema), checked_values


def _wrap_in_section(name: str, content: str) -> str:
    """``content`` enclosed in a named section that the model can tell apart from text inside it.

    The boundary marker is computed from the content and does not occur in it. So text in the
    content that looks like a closing tag does not end the section. The same content always gets
    the same marker, so the same inputs always produce the same request.
    """
    counter = 0
    while True:
        boundary = hashlib.sha256(f"{name}:{counter}:{content}".encode()).hexdigest()[:20]
        if boundary not in content:
            break
        counter += 1
    return (
        f'<judge_section name="{name}" boundary="{boundary}">\n{content}\n'
        f'</judge_section boundary="{boundary}">'
    )


def _correction_message(
    reasons: Sequence[InvalidOutputReason], output_schema: JSONObject
) -> UserMessage:
    """The message that follows a rejected answer: what was wrong, and the schema again."""
    text = "\n\n".join(
        (
            _wrap_in_section("validation_error", "\n".join(map(_reason_text, reasons))),
            _wrap_in_section(
                "task",
                "Return the assessment as one valid JSON object satisfying the schema below.",
            ),
            _output_schema_in_section(output_schema),
        )
    )
    return UserMessage(content=(ContentText(text=text),))


def _reason_text(reason: InvalidOutputReason) -> str:
    """``reason`` as text for the model.

    It gives the part of the answer as a path from ``$``, then what is wrong there. A reason
    about the answer as a whole is its description alone.
    """
    if not reason.path_in_output:
        return reason.description
    where = "".join(
        f"[{key}]" if isinstance(key, int) else f".{key}" for key in reason.path_in_output
    )
    return f"${where}: {reason.description}"


def _reason(path: Sequence[str | int], description: str) -> InvalidOutputReason:
    """A reason that can be sent back to the model and stored.

    A reason can quote what the model wrote: a field name in its path, text in its description.
    Text that is not valid Unicode is written with escapes.
    """

    def with_escapes(text: str) -> str:
        return text.encode("utf-8", "backslashreplace").decode("utf-8")

    return InvalidOutputReason(
        path_in_output=tuple(with_escapes(key) if isinstance(key, str) else key for key in path),
        description=with_escapes(description),
    )


def _read_answer(
    response: ModelResponse,
    answer_validator: Any,
    checked_values: list[JSONValue],
    entity_ids: Mapping[str, EntityID],
) -> tuple[JSONObject | None, tuple[Citation, ...], tuple[InvalidOutputReason, ...]]:
    """The answer in ``response`` with its citations, or the reasons it is not accepted.

    Returns ``(output, citations, ())`` for an accepted answer, and ``(None, (), reasons)``
    otherwise.

    Args:
        response: The model's response.
        answer_validator: The validator from ``_answer_validator``.
        checked_values: The list ``_answer_validator`` returned with it. It is emptied here and
            filled again while this answer is checked.
        entity_ids: The ID of each entity, under the name it was given.
    """
    if isinstance(response.stop_reason, StopMaxTokens):
        # Text that was cut off is not read: an earlier draft in it could pass for the answer.
        cut_off = _reason(
            (),
            "The answer was cut off because it reached the limit on output length. Write a"
            " shorter answer.",
        )
        return None, (), (cut_off,)
    output = _parse_json_object(_answer_text(response))
    if isinstance(output, str):
        return None, (), (_reason((), output),)
    checked_values.clear()
    try:
        reasons = [
            _reason(leaf.absolute_path, leaf.message)
            for error in answer_validator.iter_errors(output)
            for leaf in _most_specific(error)
        ]
        if not reasons:
            reasons.extend(_storage_problems(output, ()))
    except RecursionError:
        # Checking walks the answer level by level, and Python stops a walk that goes too deep.
        return None, (), (_reason((), "The answer is nested too deeply to check."),)
    if reasons:
        return None, (), tuple(reasons)
    # ``judge`` checks nothing about citations itself: that is for each custom format's
    # validator. It only reads the citations out, and only from fields with a custom format. Any
    # other field is free text.
    return output, find_citations_in_output(output, entity_ids, checked_values), ()


def _answer_text(response: ModelResponse) -> str:
    return "".join(
        content.text
        for message in response.output
        if isinstance(message, AssistantMessage)
        for content in message.content
        if isinstance(content, ContentText)
    )


# Reads JSON. ``strict=False`` allows a line break written as it is inside a string, which
# models often write in a long discussion.
_JSON_DECODER = json.JSONDecoder(strict=False)


def _parse_json_object(text: str) -> JSONObject | str:
    """``text`` as a JSON object, or a message saying why it is not one.

    The whole text is tried first. If that fails, the text is searched for JSON objects. This
    tolerates a Markdown code fence and other text around the object, including text with braces
    of its own. Exactly one object must be found. With several, none can be taken as the answer.
    """
    stripped = text.strip()
    whole_text_problem = "the answer is empty"
    try:
        value: Any = _JSON_DECODER.decode(stripped)
    except RecursionError:
        whole_text_problem = "it is nested too deeply"
    except ValueError as error:
        # ValueError covers text that is not JSON and a number with too many digits to read.
        if stripped:
            whole_text_problem = str(error)
    else:
        if isinstance(value, dict):
            return cast(JSONObject, value)
        return "The answer must be one JSON object, not another kind of JSON value."
    found: list[JSONObject] = []
    position = stripped.find("{")
    while position != -1:
        try:
            value, end = _JSON_DECODER.raw_decode(stripped, position)
        except (ValueError, RecursionError):
            position = stripped.find("{", position + 1)
            continue
        if isinstance(value, dict):
            found.append(cast(JSONObject, value))
        # Objects nested in this one are part of it, so the search goes on after it.
        position = stripped.find("{", end)
    if len(found) == 1:
        return found[0]
    if found:
        return (
            f"The answer has {len(found)} JSON objects. Return exactly one, with no other JSON"
            " object before or after it."
        )
    return f"The answer could not be read as one JSON object: {whole_text_problem}."


def _most_specific(error: Any) -> list[Any]:
    """The errors that say what is wrong, for one schema error.

    An error from ``anyOf`` or ``oneOf`` only says that no alternative fits. The errors inside
    it say why, so those are used. If any of them comes from a custom format's validator, all of
    those are used. Otherwise the most relevant one is used.
    """
    format_errors = [leaf for leaf in _innermost(error) if leaf.validator == "format"]
    return format_errors or [best_match([error])]


def _innermost(error: Any) -> Iterator[Any]:
    if not error.context:
        yield error
    for inner in error.context or ():
        yield from _innermost(inner)


def _storage_problems(
    value: JSONValue, path: tuple[str | int, ...]
) -> Iterator[InvalidOutputReason]:
    """Why fxtr cannot store ``value``, which is at ``path`` in the answer.

    JSON allows more than fxtr stores: fxtr needs finite numbers, integers from ``-2**64``
    through ``2**64 - 1``, and text that is valid Unicode.
    """
    if isinstance(value, str):
        if not _is_valid_unicode(value):
            yield _reason(path, "the text has a character that is not valid Unicode.")
    elif isinstance(value, bool) or value is None:
        return
    elif isinstance(value, int):
        if not -(2**64) <= value <= 2**64 - 1:
            yield _reason(path, "the integer is too large to store.")
    elif isinstance(value, float):
        if not math.isfinite(value):
            yield _reason(path, "the number must be finite, not NaN or Infinity.")
    elif isinstance(value, dict):
        for key, item in value.items():
            if not _is_valid_unicode(key):
                yield _reason(path, "a field name has a character that is not valid Unicode.")
            yield from _storage_problems(item, (*path, key))
    else:
        for index, item in enumerate(value):
            yield from _storage_problems(item, (*path, index))


def _is_valid_unicode(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True
