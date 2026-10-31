"""Launch the slow mapped experiment (:mod:`basic_project.slow_mapped`), for watching work in flight
in the viewer.

``uv run python launch_slow_mapped.py`` runs it as a Postgres job of this project;
``--items`` sets the samples per lane,
``--delay-scale`` stretches or skips the sleeps, and ``--concurrency`` bounds the steps running at
once. ``--allow-staged`` pins staged changes; ``--no-verify`` skips the environment check.
"""

import argparse
import math
import uuid

from basic_project.slow_mapped import slow_mapped_experiment
from fxtr.entity_defns.array import Array
from fxtr.project.running import launch_cli

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--allow-staged",
        action="store_true",
        help="pin uncommitted changes in the checkout as a commit of fxtr's own",
    )
    parser.add_argument(
        "--no-verify", action="store_true", help="skip the check that the venv matches the project"
    )
    parser.add_argument("--items", type=int, default=40, help="samples per lane (default: 40)")
    parser.add_argument(
        "--delay-scale", type=float, default=1.0, help="sleep multiplier; 0 skips sleeps"
    )
    parser.add_argument(
        "--concurrency", type=int, default=8, help="steps running at once (default: 8)"
    )
    args = parser.parse_args()
    if args.items < 1 or args.concurrency < 1:
        parser.error("--items and --concurrency must be positive")
    if not math.isfinite(args.delay_scale) or args.delay_scale < 0:
        parser.error("--delay-scale must be finite and nonnegative")
    # A fresh cache namespace, recorded as an input, so a rerun sleeps again and replays stably.
    inputs = {
        "items": Array.scalar(args.items, int),
        "run_id": Array.scalar(uuid.uuid4().hex, str),
        "delay_scale": Array.scalar(args.delay_scale, float),
    }

    launch_cli(
        slow_mapped_experiment,
        inputs,
        anchor=__file__,
        allow_staged=args.allow_staged,
        verify_environment=not args.no_verify,
        max_concurrency=args.concurrency,
    )
