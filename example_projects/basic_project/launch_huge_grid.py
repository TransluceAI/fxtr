"""Launch the huge grid (:mod:`basic_project.huge_grid`): 5,000 mapped step invocations by default,
for watching a large run in the viewer.

``uv run python launch_huge_grid.py`` runs it as a job of this project; ``--tasks`` and ``--seeds``
resize the grid, ``--delay-scale`` stretches or skips the sleeps, and ``--concurrency`` bounds the
steps running at once. Use ``--tasks 2 --seeds 2 --delay-scale 0`` for a small, quick Postgres
run. ``--allow-staged`` pins staged changes; ``--no-verify`` skips the environment check.
"""

import argparse
import math
import uuid

from basic_project.huge_grid import huge_grid_experiment
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
    parser.add_argument("--tasks", type=int, default=25, help="tasks per model (default: 25)")
    parser.add_argument("--seeds", type=int, default=5, help="seeds per task (default: 5)")
    parser.add_argument(
        "--delay-scale", type=float, default=1.0, help="sleep multiplier; 0 skips sleeps"
    )
    parser.add_argument(
        "--concurrency", type=int, default=16, help="steps running at once (default: 16)"
    )
    args = parser.parse_args()
    if args.tasks < 1 or args.seeds < 1 or args.concurrency < 1:
        parser.error("--tasks, --seeds, and --concurrency must be positive")
    if not math.isfinite(args.delay_scale) or args.delay_scale < 0:
        parser.error("--delay-scale must be finite and nonnegative")
    # A fresh cache namespace, recorded as an input, so a rerun sleeps again and replays stably.
    inputs = {
        "tasks": Array.scalar(args.tasks, int),
        "seeds": Array.scalar(args.seeds, int),
        "run_id": Array.scalar(uuid.uuid4().hex, str),
        "delay_scale": Array.scalar(args.delay_scale, float),
    }

    launch_cli(
        huge_grid_experiment,
        inputs,
        anchor=__file__,
        allow_staged=args.allow_staged,
        verify_environment=not args.no_verify,
        max_concurrency=args.concurrency,
    )
