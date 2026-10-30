"""Tiny self-contained HTTP server for walking an fxtr entity graph by CID.

Point it at an entity store directory (the kind returned by
``fxtr.api.fxtrconfig.get_active_entity_store``, i.e. ``<data.active>/entities``) and it serves a
debug UI: every path that is an entity CID string renders that entity's DAG-CBOR as pretty-printed,
JSON-like HTML. Any CID *leaf* (a tag-42 link inside the CBOR) becomes a clickable link to that
entity's page, so you can walk the graph starting from some known CID. Additionally, any plain
*string* field whose value starts with the CID prefix ``bafyrei`` is rendered as a link too, but in
a different colour, since it's an indirect/by-name CID reference rather than a real IPLD link.

This intentionally has no dependencies beyond fxtr itself (and the stdlib) — it does NOT need the
Node/TypeScript frontend of ``fxtr/debug_visualizer/locator_server.py``. Run it from the repo:

    uv run scripts/browse_entities.py /path/to/<data.active>/entities

It binds to a free port by default and prints the URL. Open it and either paste a CID into the box
or navigate straight to ``http://127.0.0.1:<port>/bafyrei...``.
"""

from __future__ import annotations

import argparse
import html
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import cbrrr

from fxtr.object_model import locator as locator_mod

# A real entity CID (cidv1 dag-cbor sha-256) always base32-encodes to this prefix in our system.
CID_PREFIX = "bafyrei"

# Colours: direct tag-42 CID links vs. indirect "bafyrei..."-string references.
DIRECT_LINK_COLOR = "#1a7f37"  # green
INDIRECT_LINK_COLOR = "#bc4c00"  # orange

PAGE_CSS = """
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         margin: 1.5rem auto; max-width: 1100px; padding: 0 1rem; color: #1f2328; }
  h1 { font-size: 1.1rem; }
  code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  pre { background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 6px;
        padding: 1rem; overflow-x: auto; font-size: 13px; line-height: 1.5; }
  .key { color: #0550ae; }
  .str { color: #0a3069; }
  .num { color: #953800; }
  .lit { color: #6639ba; }
  .bytes { color: #57606a; font-style: italic; }
  a.cid { color: %(direct)s; text-decoration: none; }
  a.cid-indirect { color: %(indirect)s; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .meta { color: #57606a; font-size: 12px; margin-bottom: 1rem; }
  form { margin-bottom: 1rem; }
  input[type=text] { width: 70%%; font-family: ui-monospace, monospace; padding: 4px 6px; }
""" % {
    "direct": DIRECT_LINK_COLOR,
    "indirect": INDIRECT_LINK_COLOR,
}


def _esc(s: str) -> str:
    return html.escape(s, quote=True)


def _cid_link(cid_str: str, *, indirect: bool) -> str:
    """Render a CID string as an anchor pointing at that entity's page."""
    cls = "cid-indirect" if indirect else "cid"
    href = "/" + _esc(cid_str)
    return f'<a class="{cls}" href="{href}">{_esc(cid_str)}</a>'


def render_value(value: Any, indent: int) -> str:
    """Recursively render a DAG-CBOR value as JSON-like, link-decorated HTML.

    ``value`` is the generic structure produced by ``cbor_util.decode_cbor`` (the same thing a
    locator's ``retrieve`` returns): dict / list / str / int / float / bool / None / bytes /
    ``cbrrr.CID``.
    """
    pad = "  " * indent
    child_pad = "  " * (indent + 1)

    # Direct CID leaf (CBOR tag 42) -> green link.
    if isinstance(value, cbrrr.CID):
        return _cid_link(value.encode(base="base32"), indirect=False)

    if isinstance(value, bool):  # before int, since bool is an int subclass
        return f'<span class="lit">{"true" if value else "false"}</span>'
    if value is None:
        return '<span class="lit">null</span>'
    if isinstance(value, (int, float)):
        return f'<span class="num">{_esc(repr(value))}</span>'
    if isinstance(value, bytes):
        preview = value[:32].hex()
        more = "…" if len(value) > 32 else ""
        return f'<span class="bytes">bytes({len(value)}): {preview}{more}</span>'
    if isinstance(value, str):
        # Indirect CID reference: a plain string that looks like a CID -> orange link.
        if value.startswith(CID_PREFIX):
            return _cid_link(value, indirect=True)
        return f'<span class="str">"{_esc(value)}"</span>'

    if isinstance(value, dict):
        if not value:
            return "{}"
        items = []
        for k, v in value.items():
            key_html = f'<span class="key">"{_esc(str(k))}"</span>'
            items.append(f"{child_pad}{key_html}: {render_value(v, indent + 1)}")
        return "{\n" + ",\n".join(items) + f"\n{pad}}}"

    if isinstance(value, list):
        if not value:
            return "[]"
        items = [f"{child_pad}{render_value(v, indent + 1)}" for v in value]
        return "[\n" + ",\n".join(items) + f"\n{pad}]"

    # Fallback for anything unexpected.
    return f'<span class="str">{_esc(repr(value))}</span>'


def _page(title: str, body: str) -> bytes:
    return (
        "<!DOCTYPE html>\n<html><head><meta charset='utf-8'>"
        f"<title>{_esc(title)}</title><style>{PAGE_CSS}</style></head>"
        f"<body>{body}</body></html>"
    ).encode("utf-8")


def _search_form() -> str:
    return (
        '<form action="/go" method="get">'
        '<input type="text" name="cid" placeholder="bafyrei..." autofocus> '
        "<button type=submit>open</button></form>"
    )


def make_handler(locator: locator_mod.Locator, store_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        # Quieter logging: one line per request is plenty for a debug tool.
        def log_message(self, format: str, *args: Any) -> None:
            sys.stderr.write(f"  {self.address_string()} - {format % args}\n")

        def _send_html(self, body: bytes, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # CID content is immutable, so it is always safe to cache.
            if status == 200:
                self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            path = unquote(self.path.split("?", 1)[0]).strip("/")

            if path == "" or path == "favicon.ico":
                body = _page(
                    "fxtr entity browser",
                    f"<h1>fxtr entity browser</h1>"
                    f"<div class='meta'>store: <code>{_esc(str(store_dir))}</code></div>"
                    f"{_search_form()}"
                    "<p>Enter an entity CID above, or navigate to "
                    "<code>/&lt;cid&gt;</code> directly.</p>",
                )
                self._send_html(body, status=200 if path == "" else 404)
                return

            if path == "go":
                # Tiny redirect endpoint for the search box.
                from urllib.parse import parse_qs, urlsplit

                qs = parse_qs(urlsplit(self.path).query)
                cid = (qs.get("cid") or [""])[0].strip()
                self.send_response(302)
                self.send_header("Location", "/" + cid)
                self.end_headers()
                return

            self._serve_cid(path)

        def _serve_cid(self, cid_str: str) -> None:
            try:
                cid = cbrrr.CID.decode(cid_str)
            except Exception as e:
                self._send_html(
                    _page(
                        "invalid CID",
                        f"<h1>Invalid CID</h1><pre>{_esc(str(e))}</pre>" f"{_search_form()}",
                    ),
                    status=400,
                )
                return

            if not locator.has_entity(cid):
                self._send_html(
                    _page(
                        "not found",
                        f"<h1>Entity not found</h1>"
                        f"<div class='meta'>no blob for <code>{_esc(cid_str)}</code> in this "
                        f"store</div>{_search_form()}",
                    ),
                    status=404,
                )
                return

            try:
                entity_data, _ = locator.retrieve(cid)
            except Exception as e:
                self._send_html(
                    _page("error", f"<h1>Error loading entity</h1><pre>{_esc(repr(e))}</pre>"),
                    status=500,
                )
                return

            rendered = render_value(entity_data, 0)
            body = _page(
                cid_str,
                f"<h1><a href='/' style='text-decoration:none'>&#8962;</a> entity</h1>"
                f"<div class='meta'><code>{_esc(cid_str)}</code> &mdash; "
                f"<span style='color:{DIRECT_LINK_COLOR}'>green</span> = CID link, "
                f"<span style='color:{INDIRECT_LINK_COLOR}'>orange</span> = indirect "
                f"<code>bafyrei</code> reference</div>"
                f"<pre>{rendered}</pre>",
            )
            self._send_html(body)

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "store_dir",
        type=Path,
        help="Entity store directory: a CBOR-file store (<data.active>/entities) or an LMDB store"
        " (<data.active>/entities.lmdb, detected by a data.mdb inside).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind (default 127.0.0.1).")
    parser.add_argument(
        "--port", type=int, default=0, help="Port to bind (default 0 = pick a free port)."
    )
    args = parser.parse_args()

    if not args.store_dir.is_dir():
        print(f"Not a directory: {args.store_dir}", file=sys.stderr)
        return 2

    # Pick the backend by what's in the directory: an LMDB env has a data.mdb; otherwise treat it as
    # the CID-addressed CBOR file store.
    locator: locator_mod.Locator
    if (args.store_dir / "data.mdb").exists():
        locator = locator_mod.LMDBEntityLocator(args.store_dir)
    else:
        locator = locator_mod.ClosedBinaryCBOROnDiskLocator(directory=args.store_dir)
    handler = make_handler(locator, args.store_dir.resolve())

    server = ThreadingHTTPServer((args.host, args.port), handler)
    host, port = server.server_address[0], server.server_address[1]
    print(f"Serving entity store {args.store_dir} at http://{host}:{port}/  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
