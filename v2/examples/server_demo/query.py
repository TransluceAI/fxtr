#!/usr/bin/env python3
"""Client script: connects to the server and queries entities over HTTP.

Usage:
    uv run python examples/server_demo/query.py [--url URL]

Run serve.py first in another terminal.
"""

from __future__ import annotations

import argparse

import anyio
import httpx
from demo_entities import CodeReview, ReviewerConfig, ReviewSummary

from fxtr2.experimental.http.client import HttpFxtrReadNamespace


async def main(base_url: str) -> None:
    async with httpx.AsyncClient(base_url=base_url) as client:
        async with HttpFxtrReadNamespace(client) as ns:
            # List all pins
            all_pins = await ns.list_pins()
            reviews = {k: v for k, v in all_pins.items() if k.startswith("review_")}
            configs = {k: v for k, v in all_pins.items() if k.startswith("config/")}
            summaries = {k: v for k, v in all_pins.items() if k.startswith("summary/")}

            print(f"Connected to {base_url}")
            print(f"  {len(reviews)} reviews, {len(configs)} configs, {len(summaries)} summaries")
            print()

            # Retrieve and display a few reviews
            for review_name in sorted(reviews)[:3]:
                ref = await ns.retrieve(review_name, CodeReview)
                review = await ref.load()
                print(f"--- {review_name} ---")
                print(f"  File: {review.file_path}")
                print(f"  Comments: {len(review.comments)}")
                for i, comment in enumerate(review.comments):
                    print(
                        f"    [{i}] ({comment.severity}) {comment.author}: {comment.body[:60]}..."
                    )
                print()

            # Retrieve and display summaries for review_0
            print("=== Summaries for review_0 ===")
            for config_name in sorted(configs):
                short_name = config_name.removeprefix("config/")
                pin_key = f"summary/review_0/{short_name}"
                summary_ref = await ns.try_retrieve(pin_key, ReviewSummary)
                if summary_ref is None:
                    continue
                summary = await summary_ref.load()
                print(f"  {short_name}: score={summary.overall_score:.1f}")
                print(f"    {summary.summary_text[:80]}...")

                # Follow the reference to the reviewer config
                config = await summary.reviewer_config.load()
                print(f"    (model: {config.model_name})")
                print()

            # Show that we can also retrieve configs
            print("=== Reviewer Configs ===")
            for config_name in sorted(configs):
                ref = await ns.retrieve(config_name, ReviewerConfig)
                config = await ref.load()
                print(f"  {config_name}: {config.focus_area[:60]}... ({config.model_name})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Query fxtr entities from HTTP server")
    parser.add_argument("--url", type=str, default="http://127.0.0.1:8321", help="Server URL")
    args = parser.parse_args()

    anyio.run(main, args.url)
