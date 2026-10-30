"""Headless E2E for the publishing demo: scaffold → serve → provenance round-trip.

Over the ``nix_backend`` sweep. Scaffolds the ``publishing_workdir`` template
(prepare → seed note bytes → relock the preload FOD hashes), serves it via
:func:`published_serving_context` (``provenanced=True``), and reads it back over
HTTP. Asserts:

- the served ``RootReport`` comes back **layered** (provenance threaded
  build → layer → serve);
- its ``contents`` tree embeds the 3 seeded notes + the stats + the subreport as
  figure refs, and every figure ref resolves through the serve (the closure is
  served);
- a namespace pin (``demo:note_0``) stays **bare** and its bytes load to the Note;
- the static ``--web-dist`` mount serves ``index.html`` at ``/``.

The build uses preload FODs (the seeded notes), so this exercises the build-time
rehost and runs only where a backend is configured (the sweep skips otherwise).
"""

from __future__ import annotations

import socket
from pathlib import Path

import httpx
import pytest
from fxtr2.conversion.converters.unknown_entity import UnknownEntity
from fxtr2.data.entity_core import FxtrID, RawStrongFxtrID, RawWeakFxtrID
from fxtr2.data.entity_ref_scope import EntityRef
from fxtr2.data.tsv_namespace.tsv_file import read_tsv
from fxtr2.experimental.http.client import HttpFxtrReadNamespace
from fxtr_nix_runner._backend import Backend
from fxtr_nix_runner.serve import published_serving_context
from fxtr_nix_runner.update_hashes import update_hashes
from fxtr_workdir_harness import prepare_workdir_repo

from demo_types import Note
from fxtr_report_builder import RootReport
from seed_data import seed

pytestmark = [pytest.mark.anyio, pytest.mark.slow]

_TEMPLATE = Path(__file__).resolve().parents[1] / "publishing_workdir"


def _free_port() -> int:
    """Pick a currently-free localhost TCP port (small bind race window)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


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


async def test_publishing_demo_e2e(tmp_path: Path, nix_backend: Backend) -> None:
    # Scaffold: copy the template, seed the note bytes into the repo store, and
    # relock the preload FOD hashes (builds the slots against the seeded store).
    prepared = await prepare_workdir_repo(_TEMPLATE, tmp_path / "wd")
    await seed(prepared.path)
    await update_hashes(prepared.path, backend=nix_backend)

    note_ids = {fid.id_string() for fid in read_tsv(prepared.path / "root.fxtr.tsv").values()}

    # A trivial static bundle to exercise the --web-dist mount end-to-end.
    web_dist = tmp_path / "web"
    web_dist.mkdir()
    (web_dist / "index.html").write_text("<!doctype html><title>viewer</title>")

    port = _free_port()
    async with published_serving_context(
        prepared.path, port=port, web_dist=web_dist, backend=nix_backend
    ) as server:
        async with httpx.AsyncClient(base_url=server.base_url) as client:
            async with HttpFxtrReadNamespace(client, isolate_scope=True) as ns:
                pins = await ns.list_pins()
                # Entities serve under their attr name; namespace pins as "<ns>:<key>".
                assert "report" in pins
                assert "demo:note_0" in pins

                # The served report is layered (provenance threaded build→serve).
                report_id = await ns.retrieve("report")
                assert isinstance(report_id, FxtrID)
                assert report_id._provenance_parent is not None

                report = await (await ns.retrieve("report", RootReport)).load(ns.scope)
                assert report.title == "Publishing demo report"

                # contents embed the 3 seeded notes + the stats + the subreport.
                ref_ids: set[str] = set()
                _collect_ref_ids(report.contents, ref_ids)
                assert note_ids <= ref_ids, (note_ids, ref_ids)
                assert len(ref_ids) == 5, ref_ids

                # Every figure ref resolves through the serve (its bytes are in the
                # served closure). The two non-note refs are the stats + subreport.
                non_note = ref_ids - note_ids
                assert len(non_note) == 2, non_note
                for rid in non_note:
                    ent = await EntityRef(FxtrID.from_id_string(rid), UnknownEntity).load(ns.scope)
                    assert ent is not None

                # The namespace pin stays bare; its bytes resolve to the Note.
                note_ref = await ns.retrieve("demo:note_0", Note)
                assert isinstance(note_ref, EntityRef)
                assert note_ref.entity_id._provenance_parent is None
                note = await note_ref.load(ns.scope)
                assert note.title == "Adoption climbed in Q2"

                # Raw /data on the bare note id.
                resp = await client.get(f"/data/{note_ref.entity_id.id_string()}")
                assert resp.status_code == 200

            # The static --web-dist mount serves index.html at / (same origin).
            root = await client.get("/")
            assert "viewer" in root.text
