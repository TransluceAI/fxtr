"""Report-step entrypoint for the `report-step-check` nix target.

The report step is **just a part-3 builder** (`mkFxtrFnBuilder`) whose venv
includes `fxtr_report_builder` and whose generic `env` wires the nix-built CLI
(`FXTR_REPORT_BUILDER_CLI = ${lib.fxtr.reportBuilderCli}/bin/...`). This
entrypoint reads two input entities from parent layers, embeds them as
`RenderFigure` refs in MDX via `build_root_report`, and pins the resulting
`RootReport`. `build_root_report` shells out to the wired CLI (read from the env
by `resolve_report_cli_command`) and handles provenance projection around the
subprocess. Pure builder — no remote.
"""

from __future__ import annotations

from fxtr2.data.layered_file_store.nix_builder import BuilderContext
from fxtr_report_builder import build_root_report

_MDX_SOURCE = """# Demo Report

A figure from a plain parent-layer entity:

<RenderFigure entity={props.fxtr_inputs.note} />

A figure from a part-3-derived entity:

<RenderFigure entity={props.fxtr_inputs.wrapped} />
"""


async def build(ctx: BuilderContext) -> None:
    # Two inputs from parent layers: `note` is a plain mkEntityFromPython entity,
    # `wrapped` is derived by a part-3 builder (the fn-builder layer).
    note_id = await ctx.readers["note"].retrieve("entity")
    wrapped_id = await ctx.readers["wrapped"].retrieve("wrapped")

    report = await build_root_report(
        title=ctx.spec["title"],
        summary=ctx.spec["summary"],
        source=_MDX_SOURCE,
        inputs={"note": note_id, "wrapped": wrapped_id},
    )
    ref = ctx.scope.store(report)
    await ctx.writer.pin_to_this_layer("report", ref.entity_id, ctx.scope)
