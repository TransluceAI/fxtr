"""Round-trip tests for the report entity types.

Confirms that ``RootReport`` and ``SubReport`` survive store/load
through a :class:`FxtrScope`, with MDX-compiled body fields
(modeled here as hand-rolled ``FrozenFxtrCbor`` literals)
preserved verbatim, including embedded :class:`RawWeakFxtrID`
leaves.

No MDX involvement at this stage — the MDX builder lands in a
later step.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fxtr2.data.entity_core import (
    RawWeakFxtrID,
    freeze_fxtr_cbor,
)
from fxtr2.data.entity_ref_scope import FxtrScope

from fxtr_report_builder.entities import (
    RootReport,
    SubReport,
)


@pytest.fixture
async def report_scope() -> AsyncIterator[FxtrScope]:
    """A scope inheriting from the ambient scope (typically
    GLOBAL_SCOPE during tests), so the report type entities
    (stored at module-import via ``define_type``) are reachable
    through the parent chain.
    """
    async with FxtrScope(name="report_builder_test") as scope:
        yield scope


class TestRootReport:
    @pytest.mark.anyio
    async def test_primitive_contents_roundtrip(self, report_scope: FxtrScope):
        """Tree with only HTML/text leaves round-trips."""
        contents = freeze_fxtr_cbor(
            [
                "__fragment",
                {},
                [
                    ["h1", {}, ["Demo"]],
                    ["p", {}, ["Hello, world."]],
                ],
            ]
        )
        entity = RootReport(title="Demo", summary="A short demo.", contents=contents)
        ref = entity.store()
        loaded = await ref.load()

        assert loaded.title == "Demo"
        assert loaded.summary == "A short demo."
        assert loaded.contents == contents

    @pytest.mark.anyio
    async def test_contents_with_raw_weak_ref_survives(self, report_scope: FxtrScope) -> None:
        """A RawWeakFxtrID embedded in `contents` round-trips."""
        # Build a target entity in-scope so its id is well-formed and
        # reachable for ref-walking at store time.
        target = SubReport(
            label="target",
            overview=freeze_fxtr_cbor([]),
            detail=freeze_fxtr_cbor([]),
        )
        target_ref = target.store()
        weak = RawWeakFxtrID(inner_id=target_ref.entity_id)
        target_id = target_ref.entity_id

        contents = freeze_fxtr_cbor(
            [
                "__fragment",
                {},
                [
                    [
                        "RenderFigure",
                        {"entity": weak, "figureKey": "figure-0"},
                        [],
                    ],
                    [
                        "p",
                        {},
                        [
                            "and a ",
                            [
                                "RenderLink",
                                {"entity": weak, "linkText": "related thing"},
                                [],
                            ],
                            ".",
                        ],
                    ],
                ],
            ]
        )
        entity = RootReport(title="With ref", summary="Has a figure.", contents=contents)
        ref = entity.store()
        loaded = await ref.load()

        assert loaded.contents == contents
        # Spot-check the wrapper survived as the same class.
        figure_node = loaded.contents[2][0]  # type: ignore[index]
        assert isinstance(figure_node[1]["entity"], RawWeakFxtrID)
        assert figure_node[1]["entity"].inner_id == target_id


class TestSubReport:
    @pytest.mark.anyio
    async def test_overview_and_detail_roundtrip(self, report_scope: FxtrScope) -> None:
        """Both body fields survive."""
        overview = freeze_fxtr_cbor([["p", {}, ["Short summary."]]])
        detail = freeze_fxtr_cbor(
            [
                ["h2", {}, ["Section"]],
                ["p", {}, ["Long-form prose with ", ["strong", {}, ["emphasis"]], "."]],
            ]
        )
        entity = SubReport(label="Methodology", overview=overview, detail=detail)
        ref = entity.store()
        loaded = await ref.load()

        assert loaded.label == "Methodology"
        assert loaded.overview == overview
        assert loaded.detail == detail

    @pytest.mark.anyio
    async def test_empty_body_fields_are_legal(self, report_scope: FxtrScope) -> None:
        """Empty trees round-trip — important so callers can stub fields."""
        empty = freeze_fxtr_cbor([])
        entity = SubReport(label="Stub", overview=empty, detail=empty)
        ref = entity.store()
        loaded = await ref.load()

        assert loaded.overview == empty
        assert loaded.detail == empty
