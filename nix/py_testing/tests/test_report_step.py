"""Report-step E2E: the report build is just a part-3 builder.

`report-step-check` is a `mkFxtrFnBuilder` whose venv has `fxtr_report_builder`
and whose generic `env` wires `lib.fxtr.reportBuilderCli`
(`FXTR_REPORT_BUILDER_CLI`). Its entrypoint (`py_testing.report_fixture:build`)
reads two parent-layer inputs (a plain `mkEntityFromPython` entity + a
part-3-derived `WrappedNote`), embeds them as `RenderFigure` refs via
`build_root_report` (which shells out to the wired node CLI), and pins the
`RootReport`.

We read the report back and confirm: the title/summary survived, and the
contents tree references exactly the two input entities. (The layered store is
**bare-data on disk** — provenance is preserved *through* the build by
`build_root_report` but stripped on store, per the layer format — so the
round-tripped refs are the inputs' bare data ids.)
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import anyio
import pytest

from fxtr2.data.entity_core import RawStrongFxtrID, RawWeakFxtrID
from fxtr2.data.layered_file_store.reader import LayeredFileStoreReader
from fxtr_report_builder import RootReport

_TARGET = "packages.x86_64-linux.report-step-check"


def _collect_ref_ids(value: object, out: set[str]) -> None:
    """Collect every embedded entity ref's id_string from a decoded CBOR tree."""
    if isinstance(value, (RawWeakFxtrID, RawStrongFxtrID)):
        out.add(value.inner_id.id_string())
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_ref_ids(item, out)
    elif isinstance(value, dict):
        for item in value.values():
            _collect_ref_ids(item, out)


@pytest.fixture(scope="module")
def report_layer(nix_build: Callable[[str], str]) -> str:
    return nix_build(_TARGET)


def test_report_step_references_both_inputs(report_layer: str) -> None:
    async def _read() -> None:
        # The two input pin ids live in the report layer's parent layers.
        parents = json.loads((Path(report_layer) / "parents.json").read_text())
        input_ids: set[str] = set()
        for parent in parents:
            async with LayeredFileStoreReader(parent) as preader:
                for fid in (await preader.list_pins()).values():
                    input_ids.add(fid.id_string())

        async with LayeredFileStoreReader(report_layer) as reader:
            report = await (await reader.retrieve("report", RootReport)).load()
            assert report.title == "Demo Report"
            assert report.summary.startswith("A demo report")
            ref_ids: set[str] = set()
            _collect_ref_ids(report.contents, ref_ids)
            # The MDX had two RenderFigure refs; they resolve to exactly the two
            # parent-layer inputs.
            assert len(ref_ids) == 2, ref_ids
            assert ref_ids == input_ids, (ref_ids, input_ids)

    anyio.run(_read)


def test_report_step_deterministic(report_layer: str, nix_build: Callable[[str], str]) -> None:
    """`build_root_report` output is byte-stable for fixed inputs (a JS toolchain
    is in the loop). An independent build with identical inputs must pin the same
    content-addressed RootReport id — equal ids ⇒ byte-identical report."""
    alt = nix_build("packages.x86_64-linux.report-step-check-alt")
    id_main = json.loads((Path(report_layer) / "pins.json").read_text())["report"]
    id_alt = json.loads((Path(alt) / "pins.json").read_text())["report"]
    assert id_main == id_alt, (id_main, id_alt)
