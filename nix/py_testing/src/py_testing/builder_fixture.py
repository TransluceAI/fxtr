"""Fixture entities + entrypoint for the ``mkFxtrFnBuilder`` /
``mkFxtrScriptBuilder`` nix round-trip tests.

The flake builds an *input* layer pinning :data:`input_note` (via
``mkEntityFromPython``), then runs a builder over it: :func:`build` reads that
note through ``ctx.readers["base"]``, wraps it in a :class:`WrappedNote`, and
pins the result. The test reads the built layer back and checks the wrapped
note + that its inner reference resolves through the parent layer.

The same :func:`build` entrypoint backs both the fn form (``entry =
"py_testing.builder_fixture:build"``) and the script form (``sample_build_script.py``
imports it and calls ``nix_builder.main(build)``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fxtr2.conversion.converters.mashumaro_entities import ChildRef, DataclassEntity
from fxtr2.data.layered_file_store.nix_builder import BuilderContext
from fxtr2.data.system_entities import define_type

_NS = uuid.UUID("3f2a91c4-6e7b-4d18-9c5a-1b8e0d2f4a63")


@dataclass(frozen=True)
class InputNote(DataclassEntity, fxtr_type=define_type(_NS, "builder_input_note")):
    text: str


@dataclass(frozen=True)
class WrappedNote(DataclassEntity, fxtr_type=define_type(_NS, "builder_wrapped_note")):
    label: str
    inner: ChildRef[InputNote]


# Pinned as the builder's input layer via mkEntityFromPython (resolved as the
# module attr ``py_testing.builder_fixture:input_note``).
input_note = InputNote(text="from-parent")


async def build(ctx: BuilderContext) -> None:
    """Read the parent layer's ``InputNote``, wrap it, pin the result.

    Pure builder: reads only its parent (``ctx.readers["base"]``) — no remote.
    """
    inner_ref = await ctx.readers["base"].retrieve("entity", InputNote)
    wrapped_ref = ctx.scope.store(WrappedNote(label=ctx.spec["label"], inner=inner_ref))
    await ctx.writer.pin_to_this_layer("wrapped", wrapped_ref.entity_id, ctx.scope)
