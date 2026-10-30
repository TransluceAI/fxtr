#!/usr/bin/env python3
"""Server script: stores entities in a SQLite namespace and serves them over HTTP.

Usage:
    uv run python examples/server_demo/serve.py [--port PORT] [--db PATH]
"""

from __future__ import annotations

import argparse
import os
import random
from pathlib import Path

import anyio
from demo_entities import REVIEWER_CONFIGS, generate_reviews, random_summary

from fxtr2.data.fxtr_context_api import SQLiteFxtrNamespace
from fxtr2.experimental.http.server import serve_forever

_THIS_DIR = Path(__file__).resolve().parent
_DEFAULT_DB = _THIS_DIR / "out_gitignore" / "demo.db"


async def main(port: int, db_path: str) -> None:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    ns = SQLiteFxtrNamespace(db_path, "server_demo")
    async with ns:
        scope = ns.scope

        # Check if data already exists
        existing = await ns.list_pins(prefix="review_")
        if existing:
            print(f"Found {len(existing)} existing reviews, skipping data generation.")
        else:
            print("Generating and storing entities...")

            # Store reviewer configs
            config_refs = {}
            for name, config in REVIEWER_CONFIGS.items():
                ref = scope.store(config)
                await ns.pin(f"config/{name}", ref)
                config_refs[name] = ref
                print(f"  Pinned config/{name}")

            # Store reviews and summaries
            rng = random.Random(456)
            reviews = generate_reviews()
            for review_name, review in reviews:
                review_ref = scope.store(review)
                await ns.pin(review_name, review_ref)

                # Generate a summary for each reviewer config
                for config_name, config_ref in config_refs.items():
                    summary = random_summary(rng, review_ref, config_ref)
                    summary_ref = scope.store(summary)
                    await ns.pin(f"summary/{review_name}/{config_name}", summary_ref)

            print(f"  Stored {len(reviews)} reviews with summaries for {len(config_refs)} configs")

        # Collect all pins for serving
        all_pins = await ns.list_pins()
        print(f"\nServing {len(all_pins)} pins on http://127.0.0.1:{port}")
        print(f"  Preview an entity: http://127.0.0.1:{port}/preview/{{id_string}}")
        print(f"  List all pins:     http://127.0.0.1:{port}/pins")
        print(f"  Look up a pin:     http://127.0.0.1:{port}/pin/review_0")
        print()

        await serve_forever(scope, all_pins, port)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Serve fxtr entities over HTTP")
    parser.add_argument("--port", type=int, default=8321, help="Port to serve on")
    parser.add_argument("--db", type=str, default=str(_DEFAULT_DB), help="SQLite DB path")
    args = parser.parse_args()

    anyio.run(main, args.port, args.db)
