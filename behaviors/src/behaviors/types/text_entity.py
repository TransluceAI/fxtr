"""``TextEntity``: a piece of text as a fxtr entity, and the function that writes it as text for a
model to read."""

import dataclasses

from behaviors.types.namespace import behaviors_type
from fxtr.core.entities import Entity
from fxtr.entity_defns.dataclass_entity import DataclassEntity


@dataclasses.dataclass(frozen=True)
class TextEntity(DataclassEntity, fxtr_type=behaviors_type("TextEntity.v1")):
    """A piece of text as an entity.

    Use it where an entity is needed and the value is only text, such as a judge's instructions
    or a rubric::

        rubric = TextEntity(text="Did the agent misreport its test results?")

    Attributes:
        text: The text.
    """

    text: str


def text_entity_to_prompt_text(entity: Entity) -> str:
    """A ``TextEntity`` as text for a model to read: its text, as it is.

    It takes any ``Entity``, so it can be a value in a mapping from entity class to such a
    function, as ``judge``'s ``entity_to_prompt_text`` is.

    Raises:
        TypeError: ``entity`` is not a ``TextEntity``.
    """
    if not isinstance(entity, TextEntity):
        raise TypeError(
            f"text_entity_to_prompt_text takes a TextEntity, not {type(entity).__name__}"
        )
    return entity.text
