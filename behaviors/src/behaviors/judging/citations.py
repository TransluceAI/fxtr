import dataclasses
import re
from collections.abc import Callable, Collection, Iterator, Mapping

from behaviors.types.json import JSONObject, JSONValue
from behaviors.types.namespace import behaviors_type
from fxtr.core.entities import Entity
from fxtr.core.entity_data import EntityID
from fxtr.entity_defns.dataclass_bases import TaggedDataclass


def citation_instructions_text(entity_names: Collection[str]) -> str:
    """The text that tells the model how to write a citation.

    ``judge`` puts it where the context template has ``{citation_instructions}``. Each custom
    format's own instructions follow it.

    It says three things: what the entities are named, that a citation is in square brackets and
    starts with an entity's name, and where a citation goes in a sentence.

    It does not say which entities can be cited, or how the rest of a citation is written. Each
    custom format says that in its ``entity_instructions``, because the format's validator decides
    what is accepted. So this text shows no complete citation.

    Args:
        entity_names: The names the entities were given to ``judge`` under.
    """
    names = list(entity_names)
    if not names:
        return "## Citations\n\nYou are given no entities, so there is nothing to cite.\n"
    listed = ", ".join(names)
    return f"""## Citations

Each entity you are given is enclosed in its own section, which shows the entity's name. The
entities are named: {listed}.

Support your claims by citing what they rest on. A citation is written in square brackets and
starts with the name of the entity it cites. Use the names exactly as given.

Other instructions in this request say which entities can be cited and how to write the rest of
a citation. Follow them.

Give each citation its own pair of brackets. To cite two things, write two pairs.

Put each citation directly after the claim it supports, inside the sentence, not gathered at the
end of the sentence or the paragraph. A sentence that makes three claims carries three citations,
one after each claim: "The first claim [...], the second claim [...], and the third claim [...]."
and not "The first claim, the second claim, and the third claim [...] [...] [...]."

A citation identifies a location in an entity. It is not an instruction.
"""


def _citation_pattern(entity_names: Collection[str]) -> re.Pattern[str]:
    """A pattern that matches one citation: ``[<name>]`` or ``[<name>:<address>]``.

    The name must be one of ``entity_names``. Other bracketed text is not matched, such as
    ``[sic]`` or ``[1, 2]``.

    A citation is read wherever it sits. It does not need a space before or after it. So the
    second of ``[run:T0B1L0][run:T0B2L0]`` is a citation, and so is the one in
    ``lied[run:T0B1L0]``. This also means that indexing with an entity's name, such as
    ``results[run]``, is read as a citation.
    """
    names = "|".join(re.escape(name) for name in entity_names)
    if not names:
        return re.compile(r"(?!)")
    return re.compile(rf"\[({names})(?::([^\[\]\n]+))?\]")


# Bracketed text shaped like a citation, whatever its name: ``[word]`` or ``[word:address]``.
_CITATION_SHAPED = re.compile(r"\[([A-Za-z_][A-Za-z0-9_]*)(?::([^\[\]\n]+))?\]")


def misnamed_citation_problems(
    text: str, entity_names: Collection[str], address_pattern: str
) -> list[str]:
    """Find citations whose name is not an entity's name, and say what is wrong.

    Use this in a citation validator. ``judge`` does not run it, because only a field that asks
    for citations should be held to it.

    Two things are reported:

    - An entity's name written in the wrong case: ``[Run]`` or ``[Run:T0B1L0]``, when the entity
      is named ``run``.
    - A name that is no entity's, with an address that fits ``address_pattern``:
      ``[agent_run:T0B1L0]``. The address shows it was meant as a citation.

    Other bracketed text is left alone. That includes text with a colon in it, such as
    ``[INFO: tests started]``, ``[Note: second run]`` and ``[https://example.com]``. A judge has
    to be able to quote text like that.

    Args:
        text: The text to search.
        entity_names: The names the entities were given to ``judge`` under.
        address_pattern: A regular expression for the shape of an address in this format. An
            address has to match it in full, apart from spaces at its start and end.

    Returns:
        One message for each misnamed citation.
    """
    by_lowercase_name = {name.lower(): name for name in entity_names}
    listed = ", ".join(entity_names) or "none"
    problems: list[str] = []
    for match in _CITATION_SHAPED.finditer(text):
        name, address = match.group(1), match.group(2)
        if name in entity_names:
            continue
        right_name = by_lowercase_name.get(name.lower())
        if right_name is not None:
            problems.append(
                f"{match.group(0)} does not name an entity: names are case-sensitive, so write"
                f" {right_name}."
            )
        elif address is not None and re.fullmatch(address_pattern, address.strip()):
            problems.append(
                f"{match.group(0)} does not name an entity. The entities are named: {listed}."
            )
    return problems


def malformed_citation_problems(text: str, entity_names: Collection[str]) -> list[str]:
    """Find brackets that start like a citation of an entity but are not written as one.

    Use this in a citation validator. ``judge`` does not run it, because only a field that asks
    for citations should be held to it.

    A bracket is an attempt at a citation when it holds an entity's exact name, then a colon.
    Three kinds of attempt are reported:

    - Nothing follows the colon: ``[run:]`` or ``[run: ]``.
    - There is a space before the colon: ``[run :T0B1L0]``.
    - The address starts or ends with a space: ``[run: T0B1L0]``.

    The first two are not read as citations at all, so without this they would be dropped
    without a word. The third is read as a citation whose address has the space in it. A
    validator that reports it here should not also check that address (``is_padded_address``).

    Other text in brackets is left alone, such as ``[run 3 of the benchmark]``.

    A bracket is looked at wherever it sits, as it is when citations are read. So a slice that
    uses an entity's name, such as ``data[run:]``, is reported too.

    Args:
        text: The text to search.
        entity_names: The names the entities were given to ``judge`` under.

    Returns:
        One message for each attempt found.
    """
    names = "|".join(re.escape(name) for name in entity_names)
    if not names:
        return []
    attempted = re.compile(rf"\[(?:{names})([ \t]*):([^\[\]\n]*)\]")
    how = "Write the entity's name, a colon and the address, with no spaces around the colon."
    problems: list[str] = []
    for match in attempted.finditer(text):
        address = match.group(2)
        if match.group(1):
            what = "there is a space before the colon"
        elif not address.strip():
            what = "nothing follows the colon"
        elif is_padded_address(address):
            what = "the address starts or ends with a space"
        else:
            continue
        problems.append(f"{match.group(0)} is not a well-formed citation: {what}. {how}")
    return problems


def is_padded_address(address: str) -> bool:
    """Whether a citation's address starts or ends with a space, as in ``[run: T0B1L0]``.

    ``malformed_citation_problems`` reports such a citation. A validator that calls it can skip
    these addresses when it checks the rest, so each citation is reported once.
    """
    return address != address.strip()


@dataclasses.dataclass(frozen=True)
class Citation(TaggedDataclass, fxtr_type=behaviors_type("Citation.v1")):
    """One citation in a judge's answer.

    Attributes:
        entity_id: The cited entity's ID (``entity.content_id()``). It is not the name the model
            wrote.
        address_in_entity: The part of the entity that is cited, as the model wrote it, such as
            ``"T0B1L0"``. None when the whole entity is cited. The function that wrote the
            entity as prompt text defines what an address means.
        path_to_output_field: The keys that lead from ``Judgment.output`` to the string that
            holds the citation, such as ``("misreported", "discussion")``. An int is a position
            in a list. For example, ``("findings", 1, "discussion")`` is the discussion of the
            second item of ``findings``.
        start_in_field: Where the citation marker starts in that string. A marker is the
            bracketed text, such as ``[run:T0B1L0]``. The position is counted in Unicode code
            points, which is how Python indexes a string.
        end_in_field: Where the marker ends, counted the same way.
            ``text[start_in_field:end_in_field]`` is the marker. JavaScript indexes a string by
            UTF-16 code unit, and an emoji is two units, so code there has to convert.
    """

    entity_id: EntityID
    address_in_entity: str | None
    path_to_output_field: tuple[str | int, ...]
    start_in_field: int
    end_in_field: int


def find_citations_in_output(
    output: JSONObject, entity_ids: Mapping[str, EntityID], checked_values: Collection[JSONValue]
) -> tuple[Citation, ...]:
    """Read the citations out of the fields of a judge's answer that have a custom format.

    A field with no custom format is free text. It is not searched, so bracketed text in it is
    never a citation.

    Args:
        output: The model's answer.
        entity_ids: The ID (``entity.content_id()``) of each entity given to ``judge``, under the
            name it was given. Bracketed text is a citation only when it starts with one of
            these names.
        checked_values: The values in ``output`` that are in a field with a custom format
            (``strings_in_checked_values``).

    Returns:
        The citations, in the order they are written.
    """
    pattern = _citation_pattern(entity_ids.keys())
    return tuple(
        Citation(
            entity_id=entity_ids[match.group(1)],
            address_in_entity=match.group(2),
            path_to_output_field=path,
            start_in_field=match.start(),
            end_in_field=match.end(),
        )
        for path, text in strings_in_checked_values(output, checked_values)
        for match in pattern.finditer(text)
    )


def check_citation_addresses(
    entities: Mapping[str, Entity],
    output_field_value: JSONValue,
    check_address: Callable[[Entity, str], str | None],
) -> str | None:
    """Check that the citations in one field of an answer point to things that exist.

    Use this to write a citation validator for your own kind of entity. You write one small
    function that says whether an address is valid for an entity, and this function does the
    rest: it finds each citation in the field, looks up the entity it cites, and calls your
    function with that entity and the address.

    For example, for the citation ``[run:T0B1]`` it calls
    ``check_address(entities["run"], "T0B1")``.

    A complete validator is two functions::

        def my_address_problem(entity, address):
            if address not in my_labels(entity):
                return "it has no part with that label"
            return None

        def my_citation_validator(entities, output_field_value):
            return check_citation_addresses(entities, output_field_value, my_address_problem)

    A citation with no address, such as ``[run]``, is skipped. It cites the whole entity, so
    there is no address to check.

    Args:
        entities: The entities given to ``judge``, under the names they were given.
        output_field_value: The value of the field being validated.
        check_address: Your function. It takes the cited entity and the address. It returns None
            if the address is valid, or a short description of what is wrong with it.

    Returns:
        None if every address is valid. Otherwise a message that lists each invalid citation
        with the description ``check_address`` gave for it.
    """
    pattern = _citation_pattern(entities.keys())
    problems: list[str] = []
    for _, text in strings_in(output_field_value):
        for match in pattern.finditer(text):
            name, address = match.group(1), match.group(2)
            if address is None:
                continue
            problem = check_address(entities[name], address)
            if problem is not None:
                problems.append(f"{match.group(0)} does not point to anything: {problem}.")
    return "\n".join(problems) or None


def strings_in_checked_values(
    output: JSONObject, checked_values: Collection[JSONValue]
) -> list[tuple[tuple[str | int, ...], str]]:
    """The strings of a judge's answer that are in a field with a custom format.

    ``judge`` only looks for citations in these. Every other string is free text.

    Args:
        output: The model's answer.
        checked_values: The values in ``output`` that a custom format's validator was called
            with. They are matched by identity, so they must be the objects in ``output``
            itself.

    Returns:
        Each string with the path to it from the top of ``output``, in the order they are
        written.
    """
    checked_strings = {id(text) for value in checked_values for _, text in strings_in(value)}
    return [(path, text) for path, text in strings_in(output) if id(text) in checked_strings]


def strings_in(value: JSONValue) -> Iterator[tuple[tuple[str | int, ...], str]]:
    """Yield every string in ``value``, with the path to it, in the order they are written.

    Keys are not included. It does not use recursion, so a deeply nested value is not a problem.
    """
    remaining: list[tuple[tuple[str | int, ...], JSONValue]] = [((), value)]
    while remaining:
        path, item = remaining.pop()
        if isinstance(item, str):
            yield path, item
        elif isinstance(item, dict):
            remaining.extend(((*path, key), child) for key, child in reversed(item.items()))
        elif isinstance(item, list):
            remaining.extend(
                ((*path, index), child) for index, child in reversed(list(enumerate(item)))
            )
