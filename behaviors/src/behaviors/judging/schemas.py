"""Functions that write the JSON Schema for a judge's answer.

Each function returns the schema for a complete answer. Pass it to ``judge`` as
``output_schema``.

Every answer starts with a ``discussion``, so the model writes its reasoning before its verdict.
What follows the discussion depends on the function:

- ``binary_schema``: a yes or no, ``match``.
- ``continuous_schema``: a number on a scale, ``score``.
- ``categorical_schema``: one of a list of categories, ``category``.
- ``structured_schema``: a ``value`` with a schema of your own.

Some fields need a check of their own. Text that carries citations is one example. Such a field
is written by the ``schema`` method of a ``CustomFormat`` (see ``judge.py``). Pass that schema
as the ``discussion`` of any function above, or use it as a property of a schema written by
hand. Pass the same ``CustomFormat`` to ``judge`` in ``custom_formats``.

Sometimes a field has to point at one place in an entity, such as the message where a behavior
first appears. Make it a citation field, and ask for exactly one citation, or an empty string
when there is none. The viewer draws such a field as a link that opens the entity at that place.
A number that counts to the place, such as a message's index, is drawn as a plain number.

A schema can have several citation fields that are checked in different ways. Each way is a
``CustomFormat`` with a name of its own. Write each field with its format's ``schema`` method.

To ask for several answers at once, write the outer object by hand and use these functions for
its parts. Here ``cite`` stands for a format's ``schema`` method, such as
``AGENT_RUN_LINE_CITATION_FORMAT.schema``::

    output_schema = {
        "type": "object",
        "properties": {
            "applicable": binary_schema(
                match_description="True if the rollout is applicable.",
                discussion=cite(description="Why, citing the evidence."),
            ),
            "severity": categorical_schema(
                categories=["low", "medium", "high"],
                category_description="How severe it was.",
            ),
        },
        "required": ["applicable", "severity"],
        "additionalProperties": False,
    }

List every property under ``required``. A field left out of ``required`` can be missing from the
answer, and the answer still passes.
"""

from collections.abc import Sequence

from behaviors.types.json import JSONObject

DEFAULT_DISCUSSION_DESCRIPTION = (
    "A concise account of the evidence relevant to your assessment, including important"
    " ambiguity. It is not a request to reproduce anyone's private reasoning."
)


def _discussion_schema(discussion: str | JSONObject) -> JSONObject:
    if isinstance(discussion, str):
        return {"type": "string", "description": discussion}
    return dict(discussion)


def binary_schema(
    *,
    match_description: str,
    discussion: str | JSONObject = DEFAULT_DISCUSSION_DESCRIPTION,
) -> JSONObject:
    """The schema for a yes-or-no answer: a ``discussion`` followed by a boolean ``match``.

    Args:
        match_description: What ``match`` being true means, as the model reads it.
        discussion: Either a description for a plain text ``discussion`` field, or a schema to
            use for that field. A ``CustomFormat``'s ``schema`` method writes such a schema.
    """
    return {
        "type": "object",
        "properties": {
            "discussion": _discussion_schema(discussion),
            "match": {"type": "boolean", "description": match_description},
        },
        "required": ["discussion", "match"],
        "additionalProperties": False,
    }


def continuous_schema(
    *,
    score_description: str,
    minimum: float = 0,
    maximum: float = 10,
    discussion: str | JSONObject = DEFAULT_DISCUSSION_DESCRIPTION,
) -> JSONObject:
    """The schema for a number on a scale: a ``discussion`` followed by a ``score``.

    Args:
        score_description: What the score measures and what its ends mean, as the model reads
            it.
        minimum: The lowest score allowed.
        maximum: The highest score allowed.
        discussion: Either a description for a plain text ``discussion`` field, or a schema to
            use for that field. A ``CustomFormat``'s ``schema`` method writes such a schema.
    """
    if minimum >= maximum:
        raise ValueError("continuous_schema needs minimum to be less than maximum")
    return {
        "type": "object",
        "properties": {
            "discussion": _discussion_schema(discussion),
            "score": {
                "type": "number",
                "minimum": minimum,
                "maximum": maximum,
                "description": score_description,
            },
        },
        "required": ["discussion", "score"],
        "additionalProperties": False,
    }


def categorical_schema(
    *,
    categories: Sequence[str],
    category_description: str,
    discussion: str | JSONObject = DEFAULT_DISCUSSION_DESCRIPTION,
) -> JSONObject:
    """The schema for a choice among ``categories``: a ``discussion`` followed by a ``category``.

    Args:
        categories: The allowed answers.
        category_description: What the choice means, as the model reads it.
        discussion: Either a description for a plain text ``discussion`` field, or a schema to
            use for that field. A ``CustomFormat``'s ``schema`` method writes such a schema.
    """
    if not categories:
        raise ValueError("categorical_schema needs at least one category")
    return {
        "type": "object",
        "properties": {
            "discussion": _discussion_schema(discussion),
            "category": {"enum": list(categories), "description": category_description},
        },
        "required": ["discussion", "category"],
        "additionalProperties": False,
    }


def structured_schema(
    *,
    value_schema: JSONObject,
    discussion: str | JSONObject = DEFAULT_DISCUSSION_DESCRIPTION,
) -> JSONObject:
    """The schema for an answer of your own design: a ``discussion`` followed by a ``value``.

    Args:
        value_schema: The JSON Schema that ``value`` must fit. For a count, that could be
            ``{"type": "integer", "minimum": 0}``.
        discussion: Either a description for a plain text ``discussion`` field, or a schema to
            use for that field. A ``CustomFormat``'s ``schema`` method writes such a schema.
    """
    return {
        "type": "object",
        "properties": {
            "discussion": _discussion_schema(discussion),
            "value": dict(value_schema),
        },
        "required": ["discussion", "value"],
        "additionalProperties": False,
    }
