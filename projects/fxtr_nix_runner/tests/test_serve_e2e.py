"""Nix-marked E2E for the publishing serve path (`serve.py`, over the sweep).

Builds a real fixture flake that declares a provenanced ``fxtr.entities.report``
(a pure ``mkFxtrFnBuilder { provenanced = true; }``), a pure ``fxtr.namespaces.demo``
tsv export, and a ``fxtr.uiPresets.demo`` renderer preset; serves it
through :func:`published_serving_context`; and reads it back over HTTP. The
headline assertions:

- the provenanced entity round-trips as a **layered** ``(data, prov)`` id
  (provenance survives the whole nix-build → mount → serve → wire path);
- a namespace pin stays **bare** (the tsv-namespace flow is provenance-stripping)
  and its bytes resolve through the served repo store;
- the flake renderer builds (esbuild via nix) into a loadable ``RendererEntity``
  (:func:`build_preset_table` directly), and the serve exposes a ``dispatcher`` ws
  service (``/discovery``).

Also covers :meth:`FlakeManager.list_namespace_names` / :meth:`list_preset_names`.
Reuses the manager end-to-end, so it doubles as a manager integration test. The
entity build, namespace export, and renderer build are all pure (no FOD fetch
through the rehost), so — unlike the FOD E2Es — this does not skip on the macOS
host backend.

(The dispatcher's ``resolve``/``subscribe`` ws round-trip itself is not exercised
here — there's no Python ws service client; ``build_preset_table`` covers the
build→mint path and ``/discovery`` covers the wiring. The ws transport is covered
by the TS connector-http integration tests.)
"""

from __future__ import annotations

import dataclasses
import shutil
import socket
import uuid
from pathlib import Path

import httpx
import pytest
from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, EntityRef, FxtrScope
from fxtr2.data.repo_storage import RepoEntityStore
from fxtr2.data.system_entities import define_type
from fxtr2.experimental.http.client import HttpFxtrReadNamespace
from fxtr_nix_runner._backend import Backend
from fxtr_nix_runner.manager import FlakeManager
from fxtr_nix_runner.renderers import build_preset_table
from fxtr_nix_runner.serve import published_serving_context
from fxtr_workdir_harness import prepare_workdir_repo

# The fixture's renderer preset (renderers/report/fxtr_renderer.nix): one renderer
# keyed to a builtin type + dispatch-key uuid.
_RENDERER_TYPE_ID = "!publishing_demo_report_type"
_RENDERER_KEY_UUID = "11111111-1111-1111-1111-111111111111"

pytestmark = [pytest.mark.anyio, pytest.mark.slow]

_TEMPLATE = Path(__file__).parent / "_workdir_templates" / "publishing_demo"
_NS = uuid.UUID("a8d51e2c-7f04-49b3-9c61-2e0d8b4a6f37")


@dataclasses.dataclass(frozen=True)
class NsLeaf(DataclassEntity, fxtr_type=define_type(_NS, "publishing_serve_ns_leaf")):
    value: str


def _free_port() -> int:
    """Pick a currently-free localhost TCP port (small bind race window)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def _template_fixture(dest: Path, *, entity_id: str) -> Path:
    """Copy the publishing_demo template, substituting the namespace pin's id
    (the fxtr-lib marker stays for ``prepare_workdir_repo``)."""
    shutil.copytree(_TEMPLATE, dest)
    tsv = dest / "root.fxtr.tsv"
    tsv.write_text(tsv.read_text().replace("@ENTITY_ID@", entity_id))
    return dest


async def test_serve_published_entity_and_namespace(tmp_path: Path, nix_backend: Backend) -> None:
    # The namespace pin's bytes live only in the repo store (the export is pure
    # metadata), so seed the leaf and learn its id before templating the TSV.
    async with FxtrScope(name="seed") as seed:
        leaf_id = seed.store(NsLeaf(value="published_namespace_value")).entity_id
        tmpl = _template_fixture(tmp_path / "template", entity_id=leaf_id.id_string())
        prepared = await prepare_workdir_repo(tmpl, tmp_path / "wd")
        async with RepoEntityStore.open(prepared.path, create=True) as store:
            await store.write_from_arena(
                leaf_id, seed._main_arena, seed._current_auth_lease().clone()
            )

    # Declared names are cheap evals; the renderer preset builds directly.
    async with FlakeManager(prepared.path, backend=nix_backend) as mgr:
        async with mgr.build_span() as span:
            assert await mgr.list_namespace_names(span) == ["demo"]
            assert await mgr.list_preset_names(span) == ["demo"]
            # Build the flake renderer (esbuild via nix) into a servable table; the
            # minted RendererEntity must load with non-empty compiled-ESM bytes.
            # (rev=None: live workdir, matching the manager's read mode.)
            async with FxtrScope(
                name="renderer-build-test", isolated=True, include=[GLOBAL_SCOPE]
            ) as rscope:
                table = await build_preset_table(span, rscope, None, "demo")
                entry = table[_RENDERER_TYPE_ID][_RENDERER_KEY_UUID]
                assert entry.subtype_key == "report"
                loaded = await entry.renderer.load(rscope)
                assert loaded.module  # non-empty compiled ESM bytes

    port = _free_port()
    async with published_serving_context(prepared.path, port=port, backend=nix_backend) as server:
        async with httpx.AsyncClient(base_url=server.base_url) as client:
            async with HttpFxtrReadNamespace(client, isolate_scope=True) as ns:
                pins = await ns.list_pins()
                # Entities serve under their attr name; namespace pins as "<ns>:<key>".
                assert "report" in pins
                assert "demo:greeting" in pins

                # The provenanced entity round-trips as a layered (data, prov) id.
                report = await ns.retrieve("report")
                assert isinstance(report, FxtrID)
                assert report._provenance_parent is not None

                # The namespace pin stays bare; its bytes resolve via the store.
                leaf_ref = await ns.retrieve("demo:greeting", NsLeaf)
                assert isinstance(leaf_ref, EntityRef)
                assert leaf_ref.entity_id == leaf_id
                assert leaf_ref.entity_id._provenance_parent is None
                # Load via the view's own scope (it holds the server deep-fetch
                # source; the isolated scope is not the ambient active one).
                loaded = await leaf_ref.load(ns.scope)
                assert loaded.value == "published_namespace_value"

            # The flake-built renderer dispatcher is exposed as a ws service.
            discovery = (await client.get("/discovery")).json()
            assert discovery["services"]["dispatcher"]["kind"] == "ws"
