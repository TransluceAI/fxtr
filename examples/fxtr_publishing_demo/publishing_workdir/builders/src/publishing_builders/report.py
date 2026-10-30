"""Report builder: the served ``RootReport`` over the whole graph.

Reads the three notes (bare), the stats, and the subreport (provenanced parents →
layered ids) and compiles a ``RootReport`` whose ``contents`` tree embeds all of
them as ``RenderFigure`` refs. Pinned ``provenanced = true``, so the served report
is a layered id and its embedded figure refs carry provenance (the
nested-provenance carrier the demo shows). Pure builder (reads parents only).
"""

from __future__ import annotations

from fxtr2.data.layered_file_store.nix_builder import BuilderContext

from fxtr_report_builder import build_root_report
from publishing_builders import NOTE_KEYS

_SOURCE = """# Publishing demo report

This report was assembled by nix from seeded notes, an aggregate stats step, and
a nested subreport — each figure below resolves through the HTTP connector and
dispatches to its renderer.

## Summary section

<RenderFigure entity={props.fxtr_inputs.summary} />

## Aggregate

<RenderFigure entity={props.fxtr_inputs.stats} />

## The source notes

<RenderFigure entity={props.fxtr_inputs.note_0} />

<RenderFigure entity={props.fxtr_inputs.note_1} />

<RenderFigure entity={props.fxtr_inputs.note_2} />
"""


async def build(ctx: BuilderContext) -> None:
    inputs = {key: await ctx.readers[key].retrieve(key) for key in NOTE_KEYS}
    inputs["stats"] = await ctx.readers["stats"].retrieve("stats")
    inputs["summary"] = await ctx.readers["subreport"].retrieve("subreport")

    report = await build_root_report(
        title=ctx.spec["title"],
        summary=ctx.spec["summary"],
        source=_SOURCE,
        inputs=inputs,
    )

    ref = ctx.scope.store(report)
    await ctx.writer.pin_to_this_layer("report", ref.entity_id, ctx.scope)
