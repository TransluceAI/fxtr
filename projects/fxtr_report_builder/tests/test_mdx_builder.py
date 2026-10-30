"""Tests for the MDX builder Python ↔ TS plumbing.

Stage 1 (step 5) — exercise the *echo mode* TS subprocess: no real
MDX compile, just confirm that ref inputs round-trip through the
CBOR pipeline intact. The full MDX compile arrives in step 6 and
this test file grows alongside.

These tests require the TS bundle to be built first
(``pnpm install && pnpm build`` from ``v2/ts/`` or directly from
``projects/fxtr_report_builder/ts/packages/cli/``). If the bundle is
missing, the tests skip with a clear message.
"""

from __future__ import annotations

import pathlib

import pytest
from fxtr2.data.entity_core import (
    FxtrCbor,
    FxtrID,
    RawFxtrEntityMarkerID,
    RawWeakFxtrID,
    freeze_fxtr_cbor,
)
from fxtr2.data.entity_ref_scope import FxtrScope

from fxtr_report_builder import (
    MdxBuildError,
    MdxSubprocessError,
    RootReport,
    SubReport,
    build_mdx_tree,
    build_root_report,
    build_sub_report,
)
from fxtr_report_builder._ts_dist_path import resolve_ts_dist_path
from fxtr_report_builder.entities import SUB_REPORT_TYPE_ID


def _ts_bundle_present() -> bool:
    try:
        path = resolve_ts_dist_path()
    except MdxSubprocessError:
        return False
    return path.is_file()


_SKIP_REASON = (
    "TS bundle not built. Run `pnpm install --ignore-workspace && pnpm build` in "
    "`projects/fxtr_report_builder/ts/packages/cli/` to enable these tests (the CLI "
    "is a standalone package, not in the repo pnpm workspace)."
)

pytestmark = pytest.mark.skipif(not _ts_bundle_present(), reason=_SKIP_REASON)


@pytest.fixture
async def report_scope():
    """Scope that can hold report entities (used to mint ref inputs)."""
    async with FxtrScope(name="mdx_builder_test") as scope:
        yield scope


@pytest.mark.anyio
async def test_empty_source_returns_empty_tree(report_scope: FxtrScope) -> None:
    """Empty MDX compiles to an empty document — the top-level
    `__fragment` wrapper is stripped, so the returned shape is a
    plain empty tuple (the frozen form of an empty list).
    """
    tree = await build_mdx_tree("", inputs={})
    assert tree == ()


@pytest.mark.anyio
async def test_inputs_round_trip_as_refs_through_mdx(report_scope: FxtrScope) -> None:
    """A `RawWeakFxtrID` input that the MDX source embeds via
    `<RenderFigure entity={props.fxtr_inputs.hero} />` survives
    the boundary intact (decoded back to the same wrapper class).
    """
    target = SubReport(
        label="target",
        overview=freeze_fxtr_cbor([]),
        detail=freeze_fxtr_cbor([]),
    )
    target_ref = target.store()
    weak = RawWeakFxtrID(inner_id=target_ref.entity_id)

    tree = await build_mdx_tree(
        "<RenderFigure entity={props.fxtr_inputs.hero} />\n",
        inputs={"hero": weak},
    )

    figures = _collect_nodes_of_type(tree, "RenderFigure")
    assert len(figures) == 1
    embedded = figures[0][1]["entity"]
    assert isinstance(embedded, RawWeakFxtrID)
    assert embedded.inner_id == target_ref.entity_id


def test_resolve_dist_path_returns_a_file() -> None:
    """Sanity check on the path resolver."""
    path = resolve_ts_dist_path()
    assert isinstance(path, pathlib.Path)
    assert path.is_file()


def test_sub_report_type_id_is_well_formed() -> None:
    """Confirm the type id is reachable; defends against test setup drift."""
    assert SUB_REPORT_TYPE_ID is not None


@pytest.mark.anyio
async def test_mdx_compile_basic_markdown(report_scope: FxtrScope) -> None:
    """Plain Markdown compiles to a flat list of document-level
    children (h1, p, etc). The top-level `__fragment` wrapper is
    stripped before storage.
    """
    source = "# Hello\n\nA paragraph.\n"
    tree = await build_mdx_tree(source)

    # Direct tuple of children. Each child is a [type, props, kids] node.
    assert isinstance(tree, tuple)
    type_names = [c[0] for c in tree if isinstance(c, tuple)]
    assert "h1" in type_names
    assert "p" in type_names


@pytest.mark.anyio
async def test_mdx_compile_render_figure_gets_figure_key(
    report_scope: FxtrScope,
) -> None:
    """`<RenderFigure entity={...} />` gets a stable `figureKey` injected."""
    target = SubReport(
        label="t",
        overview=freeze_fxtr_cbor([]),
        detail=freeze_fxtr_cbor([]),
    )
    target_ref = target.store()
    source = (
        "Some prose.\n\n"
        "<RenderFigure entity={props.fxtr_inputs.hero} />\n\n"
        "More prose.\n\n"
        "<RenderFigure entity={props.fxtr_inputs.hero} />\n"
    )
    tree = await build_mdx_tree(
        source, inputs={"hero": RawWeakFxtrID(inner_id=target_ref.entity_id)}
    )

    figures = _collect_nodes_of_type(tree, "RenderFigure")
    assert len(figures) == 2

    # Each figure carries the entity ref + a unique auto-assigned figureKey.
    keys = []
    for figure in figures:
        props = figure[1]
        assert isinstance(props["entity"], RawWeakFxtrID)
        assert props["entity"].inner_id == target_ref.entity_id
        assert isinstance(props["figureKey"], str)
        keys.append(props["figureKey"])

    assert keys == ["figure-0", "figure-1"]


@pytest.mark.anyio
async def test_mdx_compile_render_link(report_scope: FxtrScope) -> None:
    """`<RenderLink entity={...} linkText="x" />` emits a RenderLink node."""
    target = SubReport(
        label="t",
        overview=freeze_fxtr_cbor([]),
        detail=freeze_fxtr_cbor([]),
    )
    target_ref = target.store()

    source = 'See the <RenderLink entity={props.fxtr_inputs.note} linkText="related note" />.\n'
    tree = await build_mdx_tree(
        source, inputs={"note": RawWeakFxtrID(inner_id=target_ref.entity_id)}
    )

    # The link should appear nested inside the p.
    links = _collect_nodes_of_type(tree, "RenderLink")
    assert len(links) == 1
    props = links[0][1]
    assert isinstance(props["entity"], RawWeakFxtrID)
    assert props["entity"].inner_id == target_ref.entity_id
    assert props["linkText"] == "related note"
    # Links don't get figureKey assignment.
    assert "figureKey" not in props


@pytest.mark.anyio
async def test_mdx_compile_error_surfaces_as_build_error(
    report_scope: FxtrScope,
) -> None:
    """An MDX source that can't compile raises `MdxBuildError`."""
    # Unclosed JSX tag.
    source = "<div>unclosed"
    with pytest.raises(MdxBuildError):
        await build_mdx_tree(source)


@pytest.mark.anyio
async def test_build_root_report_accepts_entity_ref(report_scope: FxtrScope) -> None:
    """`build_root_report` accepts `EntityRef[...]` inputs directly."""
    target = SubReport(
        label="t",
        overview=freeze_fxtr_cbor([]),
        detail=freeze_fxtr_cbor([]),
    )
    target_ref = target.store()

    report = await build_root_report(
        title="My Report",
        summary="A demo.",
        source="<RenderFigure entity={props.fxtr_inputs.sub} />\n",
        inputs={"sub": target_ref},
    )

    assert isinstance(report, RootReport)
    assert report.title == "My Report"
    assert report.summary == "A demo."
    figures = _collect_nodes_of_type(report.contents, "RenderFigure")
    assert len(figures) == 1
    props = figures[0][1]
    assert isinstance(props["entity"], RawWeakFxtrID)
    assert props["entity"].inner_id == target_ref.entity_id

    # The built entity stores + loads round-trip cleanly.
    ref = report.store()
    loaded = await ref.load()
    assert loaded.title == "My Report"
    assert loaded.contents == report.contents


@pytest.mark.anyio
async def test_build_sub_report_compiles_both_bodies(
    report_scope: FxtrScope,
) -> None:
    """`build_sub_report` compiles both overview + detail."""
    sub = await build_sub_report(
        label="Methodology",
        overview_source="Short.\n",
        detail_source="Long.\n\nWith multiple paragraphs.\n",
    )
    assert isinstance(sub, SubReport)
    assert sub.label == "Methodology"
    # Overview has one p; detail has two ps.
    overview_ps = _collect_nodes_of_type(sub.overview, "p")
    detail_ps = _collect_nodes_of_type(sub.detail, "p")
    assert len(overview_ps) == 1
    assert len(detail_ps) == 2


@pytest.mark.anyio
async def test_build_root_report_accepts_fxtr_id_input(
    report_scope: FxtrScope,
) -> None:
    """A bare `FxtrID` input wraps into `RawWeakFxtrID` automatically."""
    target = SubReport(
        label="t",
        overview=freeze_fxtr_cbor([]),
        detail=freeze_fxtr_cbor([]),
    )
    target_ref = target.store()

    report = await build_root_report(
        title="T",
        summary="S",
        source="<RenderFigure entity={props.fxtr_inputs.sub} />\n",
        inputs={"sub": target_ref.entity_id},  # bare FxtrID
    )

    figures = _collect_nodes_of_type(report.contents, "RenderFigure")
    assert len(figures) == 1
    assert isinstance(figures[0][1]["entity"], RawWeakFxtrID)


_GROUND = FxtrID.from_tag_and_payload(".", b"")


def _make_c_node(scope: FxtrScope, ctx_key: str = "report_builder_test") -> FxtrID:
    """Build a c-node in ``scope`` that unwraps to ``"."``.

    Mirrors the helper in ``v2/tests/test_entity_ref_scope/test_provenance_context.py`` —
    lets a test mint a layered provenance context without depending on
    other ambient state.
    """
    ctx_type = scope.track_builtin(FxtrID.builtin("ctx"))
    context_data: FxtrCbor = {
        "$fxtr.ctx": RawFxtrEntityMarkerID(inner_id=_GROUND),
        "$fxtr.type": RawWeakFxtrID(inner_id=ctx_type),
        "value": RawWeakFxtrID(inner_id=_GROUND),
        "ctx_key": ctx_key,
    }
    return scope.create_c_node(context_data)


@pytest.mark.anyio
async def test_provenance_round_trips_through_subprocess() -> None:
    """A layered ``(data, prov)`` input survives the TS boundary with
    provenance intact — the parent is re-embedded on the way back.

    The MDX subprocess CBOR codec only carries the bare ``_raw_data_id``
    of each FxtrID across the wire, so without the lift-into-prov /
    embed-on-return wrapping this assertion would fail (the returned
    ref's ``_provenance_parent`` would be ``None``).
    """
    async with FxtrScope(name="prov_outer") as outer:
        c_node = _make_c_node(outer)
        async with FxtrScope(
            name="prov_inner",
            provenance_context=c_node,
            provenance_context_from_scope=outer,
        ) as scope:
            target = SubReport(
                label="t",
                overview=freeze_fxtr_cbor([]),
                detail=freeze_fxtr_cbor([]),
            )
            target_ref = target.store()
            inner_id = target_ref.entity_id
            # Sanity: storing in a scope with a c-node marker should
            # produce a layered FxtrID. If this ever changes we want
            # to know — the test is meaningless without a layered input.
            assert inner_id._provenance_parent is not None, (
                "test setup expected store() in a scope with"
                " provenance_context to yield a layered FxtrID"
            )

            tree = await build_mdx_tree(
                "<RenderFigure entity={props.fxtr_inputs.hero} />\n",
                inputs={"hero": RawWeakFxtrID(inner_id=inner_id)},
            )

            figures = _collect_nodes_of_type(tree, "RenderFigure")
            assert len(figures) == 1
            embedded = figures[0][1]["entity"]
            assert isinstance(embedded, RawWeakFxtrID)
            # Data half preserved (FxtrID.__eq__ compares _raw_data_id):
            assert embedded.inner_id == inner_id
            # And — the point of the test — provenance preserved too:
            assert embedded.inner_id._provenance_parent == inner_id._provenance_parent
            # We're back in `scope` so the round-tripped IDs should be
            # tracked here.
            assert scope.is_tracked(embedded.inner_id)


def _collect_nodes_of_type(tree: object, target_type: str) -> list[tuple]:  # type: ignore[type-arg]
    """Walk a flattened tree and return all nodes whose type matches.

    Handles two shapes uniformly:
    - A document tree: ``(child, child, ...)`` — an array of nodes.
    - A single node: ``("type", props, children)`` — a 3-tuple.
    """
    found: list[tuple] = []  # type: ignore[type-arg]

    def walk(node: object) -> None:
        if not isinstance(node, tuple):
            return
        if len(node) == 3 and isinstance(node[0], str):
            if node[0] == target_type:
                found.append(node)
            for child in node[2]:
                walk(child)
            return
        # Not a node-shape tuple — treat as a list of children.
        for child in node:
            walk(child)

    walk(tree)
    return found
