# /// script
# requires-python = ">=3.13,<3.14"
# dependencies = [
#     "fxtr @ git+ssh://git@github.com/TransluceAI/fxtr@fxtr1_durable_cache_migration",
#     "tqdm",
# ]
# ///
"""Build the durable run-graph cache for an fxtr runset, copying SOURCE -> TARGET.

This drives ``RunGraphCacheBuilder`` (the same code the session manager uses on a cold open) to
build a fresh, byte-compatible ``active.sqlite`` (runset rows + ``_rg/`` cache rows) at ``--target``
from a read-only ``--source`` runset + the entity store (located via the config). It is:

  - **fast**: per-node extraction is parallel (threads, or ``--stage1-processes N`` for a process
    pool) and never loads run provenance;
  - **resumable / syncable**: re-run it to continue after an interruption, or to pick up runs
    *added* to the source since the last pass (both stages skip work already in their output).

The SOURCE is only ever read — other jobs can keep using it while this runs. Nothing is swapped
automatically: to migrate in place, build to a separate ``--target`` and then swap it over your
active runset yourself (after a final pass once the source has stopped changing, so the target is
fully caught up).

Run standalone with uv (no checkout needed — fxtr is pulled from the pinned branch):

    uv run scripts/migrate_runset_cache.py /path/to/fxtr.toml \
        --source /path/to/active.sqlite --target /path/to/active.cached.sqlite

``config`` supplies the entity store (``<data.active>/entities``); ``--source`` is the runset to
copy (commonly the config's own ``active.sqlite``). Both ``--source`` and ``--target`` are required
so the operation is always explicit (never an accidental in-place rewrite).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="Path to the fxtr config TOML (entity store).")
    parser.add_argument(
        "--source",
        type=Path,
        required=True,
        help="Read-only source runset sqlite to copy from (e.g. the config's active.sqlite).",
    )
    parser.add_argument(
        "--target",
        type=Path,
        required=True,
        help="Destination file to build (a fresh, byte-compatible active.sqlite). Re-running"
        " resumes into it, skipping rows already present.",
    )
    parser.add_argument(
        "--staging",
        type=Path,
        default=None,
        help="Staging file for the stage-1 checkpoint (default: <target>.staging). Kept across"
        " runs so a later run extracts only newly-added source runs (incremental sync); pass"
        " --cleanup-staging on your final run to delete it.",
    )
    parser.add_argument(
        "--cleanup-staging",
        action="store_true",
        help="Delete the staging file when this run finishes. Use on the final run; without it the"
        " staging file is kept so subsequent runs can sync only the newly-added runs cheaply.",
    )
    parser.add_argument(
        "--skip-unbuildable",
        action="store_true",
        help="Instead of aborting on a runset entry whose entities cannot be loaded, skip it AND"
        " its transitive dependent closure (keeping the result dependency-closed) and report the"
        " skipped set. The target will be a valid but smaller runset.",
    )
    parser.add_argument(
        "--stage1-processes",
        type=int,
        default=None,
        help="Run stage-1 extraction in a process pool of this many workers (instead of threads),"
        " to side-step the GIL on dependency-walk / entity-decode work.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.config.exists():
        print(f"Config not found: {args.config}", file=sys.stderr)
        return 2
    if not args.source.exists():
        print(f"Source runset not found: {args.source}", file=sys.stderr)
        return 2

    # Imported here so --help works without the (git-pulled) fxtr install resolving first.
    from fxtr.api import api_lib, fxtrconfig
    from fxtr.api.session_manager import RunGraphCacheBuilder
    from fxtr.object_model import locator

    config = fxtrconfig.parse_fxtr_config(args.config)
    staging_path = args.staging or args.target.with_name(args.target.name + ".staging")

    print(f"Config (entity store): {args.config}")
    print(f"Source (read-only):    {args.source}")
    print(f"Target:                {args.target}")
    print(f"Staging:               {staging_path}")
    if args.target.exists():
        print("Target already exists; resuming (rows already present are skipped).")

    source_runs = api_lib._load_runset_from_sqlitedict(args.source).runs
    loader = locator.CachingEntityLoader.from_locator(api_lib.get_active_data_locator(config))

    print(f"Building cache for {len(source_runs)} source runs ...")
    builder = RunGraphCacheBuilder(
        source_runs=source_runs,
        loader=loader,
        staging_path=staging_path,
        dest_path=args.target,
        copy_runset=True,
        skip_unbuildable=args.skip_unbuildable,
        use_processes=args.stage1_processes is not None,
        max_workers=args.stage1_processes,
        keep_staging=not args.cleanup_staging,
    )
    built, skipped = builder.run()

    print(
        f"Done. Built {built} nodes this run into {args.target} (from {len(source_runs)} source"
        f" runs{f'; {skipped} skipped' if skipped else ''})."
    )
    if skipped:
        print(
            f"WARNING: {skipped} node(s) (unbuildable + their dependents) were skipped; the target"
            " is a smaller, dependency-closed runset.",
            file=sys.stderr,
        )
    if args.cleanup_staging:
        print(f"Staging deleted ({staging_path}).")
    else:
        print(
            f"Staging kept at {staging_path} — re-run to sync runs added to the source since this"
            " pass (only the new ones are extracted). Pass --cleanup-staging on your final run, or"
            " delete it once you no longer need to sync."
        )
    print(
        "Source was not modified. Once the target is fully built (re-run after the source stops"
        " changing so target == source), swap it into place as the new active.sqlite yourself."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
