"""Subreport builder: an MDX ``SubReport`` over a couple of notes + the stats.

Reads the stats (a provenanced parent → layered id) and two notes (bare) and
compiles them into a ``SubReport`` via ``build_sub_report``, which shells out to
the env-wired report CLI (``FXTR_REPORT_BUILDER_CLI``) and projects each figure
ref's provenance around the subprocess. Pure builder (reads parents only); pinned
``provenanced = true`` so the subreport itself reads back layered.
"""

from __future__ import annotations

from fxtr2.data.layered_file_store.nix_builder import BuilderContext

from fxtr_report_builder import build_sub_report

_OVERVIEW_SOURCE = """Aggregate statistics for this batch of notes:

<RenderFigure entity={props.fxtr_inputs.stats} />
"""

_DETAIL_SOURCE = """The first two notes that fed the aggregate:

<RenderFigure entity={props.fxtr_inputs.note_a} />

<RenderFigure entity={props.fxtr_inputs.note_b} />
"""


async def build(ctx: BuilderContext) -> None:
    stats_id = await ctx.readers["stats"].retrieve("stats")
    note_a = await ctx.readers["note_0"].retrieve("note_0")
    note_b = await ctx.readers["note_1"].retrieve("note_1")

    sub = await build_sub_report(
        label=ctx.spec["label"],
        overview_source=_OVERVIEW_SOURCE,
        detail_source=_DETAIL_SOURCE,
        overview_inputs={"stats": stats_id},
        detail_inputs={"note_a": note_a, "note_b": note_b},
    )

    ref = ctx.scope.store(sub)
    await ctx.writer.pin_to_this_layer("subreport", ref.entity_id, ctx.scope)
