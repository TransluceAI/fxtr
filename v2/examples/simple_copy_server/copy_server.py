#!/usr/bin/env python3
"""Mirror a remote fxtr namespace into a local SQLite database, then serve it.

Subcommands:
    copy_in  -- Fetch all pins from a remote HTTP namespace and copy them into
                 a local SQLite namespace. Pins present locally but absent on
                 the remote are unpinned (full sync).
    serve    -- Serve the local SQLite namespace over HTTP.

Usage:
    cd v2
    uv run python -m examples.simple_copy_server.copy_server copy_in http://127.0.0.1:8321 /tmp/mirror.db
    uv run python -m examples.simple_copy_server.copy_server serve /tmp/mirror.db --port 8322
"""

from __future__ import annotations

import argparse
import os

import anyio
import httpx

from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace
from fxtr2.experimental.http.client import HttpFxtrReadNamespace
from fxtr2.experimental.http.server import serve_forever


async def do_copy_in(url: str, db_path: str, namespace: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    async with httpx.AsyncClient(base_url=url) as client:
        async with HttpFxtrReadNamespace(client) as http_ns:
            remote_pins = await http_ns.list_pins()
            print(f"Remote has {len(remote_pins)} pins")

            async with SQLiteFxtrNamespace(db_path, namespace) as sqlite_ns:
                local_pins = await sqlite_ns.list_pins()

                # Copy each remote pin into the local namespace.
                for key in remote_pins:
                    # Retrieve from HTTP (tracked in http_ns.scope, visible
                    # to the active scope since sqlite_ns is nested inside).
                    fxtr_id = await http_ns.retrieve(key)
                    await sqlite_ns.pin(key, fxtr_id, overwrite=True)
                    print(f"  pinned {key}")

                # Unpin local pins that no longer exist on the remote.
                stale_keys = set(local_pins) - set(remote_pins)
                for key in sorted(stale_keys):
                    await sqlite_ns.unpin(key)
                    print(f"  unpinned {key}")

            print("Done.")


async def do_serve(db_path: str, namespace: str, port: int) -> None:
    async with SQLiteFxtrNamespace(db_path, namespace) as ns:
        all_pins = await ns.list_pins()
        print(f"Serving {len(all_pins)} pins on http://127.0.0.1:{port}")
        await serve_forever(ns.scope, all_pins, port)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mirror a remote fxtr namespace into SQLite and serve it."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    cp = sub.add_parser("copy_in", help="Copy pins from a remote HTTP namespace into SQLite")
    cp.add_argument("url", help="Base URL of the remote fxtr HTTP server")
    cp.add_argument("db_path", help="Path to the local SQLite database")
    cp.add_argument("--namespace", default="mirror", help="Namespace name (default: mirror)")

    sv = sub.add_parser("serve", help="Serve the local SQLite namespace over HTTP")
    sv.add_argument("db_path", help="Path to the local SQLite database")
    sv.add_argument("--namespace", default="mirror", help="Namespace name (default: mirror)")
    sv.add_argument("--port", type=int, default=8322, help="Port to serve on (default: 8322)")

    args = parser.parse_args()

    if args.command == "copy_in":
        anyio.run(do_copy_in, args.url, args.db_path, args.namespace)
    elif args.command == "serve":
        anyio.run(do_serve, args.db_path, args.namespace, args.port)


if __name__ == "__main__":
    main()
