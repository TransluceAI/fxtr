"""Tool names across dialects: a grouped tool keeps its own name in a dialect without namespaces
unless another declaration shares it, and a call made under that name resolves back."""

import pytest

from behaviors.implementations.dialects._common import ToolNames, wire_tool_names
from behaviors.interfaces.dialect_codecs import CodecError
from behaviors.types.tool import ToolParam


def _tool(name: str, namespace: str | None = None, freeform: bool = False) -> ToolParam:
    return ToolParam(
        name=name,
        description=None,
        input_schema={"type": "object"},
        strict=False,
        input_examples=None,
        namespace=namespace,
        freeform_format={"type": "text"} if freeform else None,
    )


def test_grouped_tools_keep_their_names_unless_they_collide() -> None:
    tools = (
        _tool("shell"),
        _tool("exec", "functions", freeform=True),
        _tool("spawn", "collaboration"),
        _tool("status", "collaboration"),
        _tool("status", "workflows"),
    )
    assert wire_tool_names(tools) == {
        (None, "shell"): "shell",
        ("functions", "exec"): "exec",
        ("collaboration", "spawn"): "spawn",
        ("collaboration", "status"): "collaboration__status",
        ("workflows", "status"): "workflows__status",
    }


def test_a_call_resolves_by_group_or_by_wire_name() -> None:
    tools = (
        _tool("exec", "functions", freeform=True),
        _tool("status", "collaboration"),
        _tool("status", "workflows"),
        _tool("plain"),
    )
    names = ToolNames(tools)
    # A call that carries its group (a Responses subject) resolves by (group, name).
    assert names.resolve("status", "workflows") is tools[2]
    # A call by wire name (a subject in a dialect without namespaces) resolves through the map.
    assert names.resolve("exec", None) is tools[0]
    assert names.resolve("collaboration__status", None) is tools[1]
    assert names.resolve("plain", None) is tools[3]
    # An ambiguous bare name resolves to nothing rather than to a guess.
    assert names.resolve("status", None) is None
    assert names.resolve("unknown", None) is None
    assert names.wire_name("status", "workflows") == "workflows__status"
    assert names.wire_name("exec", "functions") == "exec"
    assert names.wire_name("never_declared", "ns") == "never_declared"


def test_a_dialect_that_flattens_refuses_two_tools_with_one_wire_name() -> None:
    # `a.status` flattens to `a__status`, which a top-level tool already claims.
    tools = (_tool("status", "a"), _tool("status", "b"), _tool("a__status"))
    with pytest.raises(CodecError, match="'a__status' for a.status and a__status"):
        ToolNames(tools, flattened=True)
    # A dialect with namespaces keeps the three apart by group and never flattens; the shared
    # wire name resolves to neither rather than to a guess.
    names = ToolNames(tools)
    assert names.resolve("status", "a") is tools[0]
    assert names.resolve("a__status", None) is tools[2]  # the top-level tool, by its own name
    assert names.resolve("b__status", None) is tools[1]


def test_a_forced_choice_resolves_without_guessing() -> None:
    tools = (_tool("run", "a"), _tool("run", "b"), _tool("go", "a"), _tool("go"))
    names = ToolNames(tools)
    assert names.resolve_choice("go") is tools[3]  # the top-level tool wins over a grouped one
    assert names.resolve_choice("run") is None  # two groups: no guess
    assert names.resolve_choice("never") is None
    grouped_only = ToolNames(tools[:3])
    assert grouped_only.resolve_choice("go") is tools[2]
    assert grouped_only.choice_wire_name("go") == "go"
    assert grouped_only.choice_wire_name("never") == "never"
    with pytest.raises(CodecError, match="declared in several namespaces"):
        grouped_only.choice_wire_name("run")


def test_a_call_resolves_to_one_identity_with_how_it_was_named() -> None:
    tools = (
        _tool("status", "collaboration"),
        _tool("status", "workflows"),
        _tool("spawn", "collaboration"),
        _tool("plain"),
    )
    names = ToolNames(tools)
    carried = names.resolve_call("status", "workflows")
    assert (carried.tool, carried.name, carried.namespace, carried.how) == (
        tools[1],
        "status",
        "workflows",
        "namespaced",
    )
    # A flattened alias un-flattens its name and restores its group together.
    alias = names.resolve_call("workflows__status", None)
    assert (alias.tool, alias.name, alias.namespace, alias.how) == (
        tools[1],
        "status",
        "workflows",
        "alias",
    )
    own = names.resolve_call("spawn", None)
    assert (own.tool, own.name, own.namespace, own.how) == (
        tools[2],
        "spawn",
        "collaboration",
        "own",
    )
    top = names.resolve_call("plain", None)
    assert (top.tool, top.name, top.namespace, top.how) == (tools[3], "plain", None, "own")
    unknown = names.resolve_call("status", None)  # ambiguous bare name
    assert (unknown.tool, unknown.name, unknown.namespace, unknown.how) == (
        None,
        "status",
        None,
        "unknown",
    )
    undeclared = names.resolve_call("nope", "ns")
    assert (undeclared.tool, undeclared.namespace, undeclared.how) == (None, "ns", "namespaced")
