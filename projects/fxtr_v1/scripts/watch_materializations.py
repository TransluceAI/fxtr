# /// script
# requires-python = ">=3.13,<3.14"
# dependencies = [
#     "fxtr @ git+ssh://git@github.com/TransluceAI/fxtr@fxtr1_durable_cache_migration",
#     "watchdog",
# ]
# ///
"""Watch an fxtr runset's materialization records and print a human-readable summary of each.

The session manager writes one ``<request_timestamp>.json`` file per materialization request into
``<data.active>/state/materializations/`` (see ``_save_materialization_record`` in
``fxtr/api/session_manager.py``). Each file just holds the CID of a ``MaterializationRecord`` entity
in the entity store; the record in turn references the tasks/workflows that were run (its
``completed`` / ``cached`` / ``failed`` / ``cancelled`` maps of ``task_cid -> run_cid``).

This script tails that directory: for every record (existing ones first, then new ones as they
land) it loads the record + the referenced tasks and prints, per task, the **function name** (the
plan's entry-point ``constructor`` string, e.g. ``behaviors.protocols.base.tasks:rollout``) and the
**arguments** it was bound with (the plan ``kwargs``), summarized/truncated so the output stays
readable. Run it in a second terminal alongside a noisy materialization job to see what is actually
being executed.

Run standalone with uv (no checkout needed — fxtr is pulled from the pinned branch):

    uv run scripts/watch_materializations.py /path/to/fxtr.toml

Useful flags: ``--depth`` (how deep to expand argument values), ``--max-value-len`` (per-value
truncation), ``--show-cached`` (also show cache-hit tasks, not just freshly run ones), ``--poll``
(use a polling watcher for network filesystems), ``--no-catchup`` (only show records that arrive
after startup).
"""

from __future__ import annotations

import argparse
import json
import logging
import queue
import sys
import time
from collections.abc import Mapping, Sequence, Set
from pathlib import Path
from typing import Any

logger = logging.getLogger("watch_materializations")


# --------------------------------------------------------------------------------------------------
# Value summarization
# --------------------------------------------------------------------------------------------------


def _short_cid(cid: Any) -> str:
    """Short, copy-pasteable base32 CID fragment."""
    try:
        s = cid.encode("base32")
    except Exception:
        s = str(cid)
    return s if len(s) <= 16 else f"{s[:8]}…{s[-4:]}"


def _truncate(s: str, max_len: int) -> str:
    s = s.replace("\n", "\\n")
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


def _plan_ctor_and_kwargs(value: Any) -> tuple[str, Mapping[str, Any]] | None:
    """If ``value`` looks like a PyImplPlan (has an entry-point constructor + kwargs), return them.

    Done by duck typing so the script does not depend on the exact plan class and degrades on
    unfamiliar plan types.
    """
    ctor = getattr(value, "constructor", None)
    kwargs = getattr(value, "kwargs", None)
    if isinstance(ctor, str) and isinstance(kwargs, Mapping):
        return ctor, kwargs
    return None


def summarize_value(core: Any, value: Any, *, depth: int, max_len: int, max_items: int) -> str:
    """Produce a compact, bounded summary of an arbitrary IPLD-able argument value."""
    # Scalars.
    if value is None or isinstance(value, (bool, int, float)):
        return repr(value)
    if isinstance(value, str):
        return _truncate(repr(value), max_len)
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"

    # A plan bound as an argument: show its function + (optionally) its own kwargs. This is the
    # common, useful case — args are usually factory plans.
    plan = _plan_ctor_and_kwargs(value)
    if plan is not None:
        ctor, kwargs = plan
        if depth <= 0 or not kwargs:
            extra = "" if not kwargs else f", +{len(kwargs)} kwargs"
            return f"plan<{ctor}>({_short_cid(_safe_cid(core, value))}{extra})"
        inner = ", ".join(
            f"{k}={summarize_value(core, v, depth=depth - 1, max_len=max_len, max_items=max_items)}"
            for k, v in list(kwargs.items())[:max_items]
        )
        if len(kwargs) > max_items:
            inner += f", …+{len(kwargs) - max_items}"
        return f"plan<{ctor}>({inner})"

    # Any other fxtr entity: name + CID, plus a peek at scalar-ish fields when we have depth budget.
    cid = _safe_cid(core, value)
    if cid is not None:
        name = type(value).__name__
        if depth > 0:
            fields = _scalar_fields(value, max_items=max_items)
            if fields:
                rendered = ", ".join(
                    f"{k}={summarize_value(core, v, depth=depth - 1, max_len=max_len, max_items=max_items)}"
                    for k, v in fields
                )
                return f"{name}#{_short_cid(cid)}({rendered})"
        return f"{name}#{_short_cid(cid)}"

    # Containers.
    if isinstance(value, Mapping):
        if depth <= 0:
            return f"{{…{len(value)} items}}"
        items = list(value.items())[:max_items]
        body = ", ".join(
            f"{_truncate(repr(k), 40)}: {summarize_value(core, v, depth=depth - 1, max_len=max_len, max_items=max_items)}"
            for k, v in items
        )
        if len(value) > max_items:
            body += f", …+{len(value) - max_items}"
        return "{" + body + "}"
    if isinstance(value, (Sequence, Set)) and not isinstance(value, (str, bytes)):
        seq = list(value)
        if depth <= 0:
            return f"[…{len(seq)} items]"
        body = ", ".join(
            summarize_value(core, v, depth=depth - 1, max_len=max_len, max_items=max_items)
            for v in seq[:max_items]
        )
        if len(seq) > max_items:
            body += f", …+{len(seq) - max_items}"
        return "[" + body + "]"

    return _truncate(repr(value), max_len)


def _safe_cid(core: Any, value: Any) -> Any:
    try:
        return core.entity_cid(value)
    except Exception:
        return None


def _scalar_fields(value: Any, *, max_items: int) -> list[tuple[str, Any]]:
    """Best-effort extraction of an entity's small scalar fields for a one-line peek."""
    out: list[tuple[str, Any]] = []
    names: Sequence[str]
    if hasattr(type(value), "__dataclass_fields__"):
        names = list(type(value).__dataclass_fields__)  # type: ignore[attr-defined]
    else:
        return out
    for name in names:
        if name.startswith("_"):
            continue
        try:
            v = getattr(value, name)
        except Exception:
            continue
        if v is None or isinstance(v, (bool, int, float, str)):
            out.append((name, v))
        if len(out) >= max_items:
            break
    return out


# --------------------------------------------------------------------------------------------------
# Record rendering
# --------------------------------------------------------------------------------------------------


def render_task(
    core: Any,
    loader: Any,
    task_cid: Any,
    run_cid: Any,
    status: str,
    *,
    depth: int,
    max_len: int,
    max_items: int,
) -> str:
    """One block describing a single task/workflow that was part of the materialization."""
    head = f"  [{status:9}] task={_short_cid(task_cid)} run={_short_cid(run_cid)}"
    try:
        task = loader(task_cid)
    except Exception as e:  # entity missing / unregistered type / etc. — degrade, don't crash.
        return head + f"\n      <could not load task entity: {type(e).__name__}: {e}>"

    kind = type(task).__name__
    fn = getattr(task, "function", None)
    tag = getattr(task, "tag", None)
    lines = [head + f"  ({kind})"]
    if tag is not None:
        lines.append(f"      tag = {_truncate(repr(tag), max_len)}")

    plan = _plan_ctor_and_kwargs(fn) if fn is not None else None
    if plan is None:
        lines.append(f"      fn  = <unrecognized plan: {type(fn).__name__}>")
        return "\n".join(lines)

    ctor, kwargs = plan
    lines.append(f"      fn  = {ctor}")
    if not kwargs:
        lines.append("      args= ()")
    else:
        lines.append("      args:")
        for k, v in kwargs.items():
            try:
                summary = summarize_value(
                    core, v, depth=depth, max_len=max_len, max_items=max_items
                )
            except Exception as e:
                summary = f"<error summarizing: {type(e).__name__}: {e}>"
            lines.append(f"        {k} = {summary}")
    return "\n".join(lines)


def render_record(
    core: Any,
    loader: Any,
    record_file: Path,
    *,
    depth: int,
    max_len: int,
    max_items: int,
    show_cached: bool,
) -> str:
    """Load a record JSON pointer + its entity and render the whole thing."""
    import cbrrr

    with open(record_file) as f:
        meta = json.load(f)
    cid_str = meta["materialization_record_cid"]
    record = loader(cbrrr.CID.decode(cid_str))

    completed = dict(getattr(record, "completed", {}) or {})
    cached = dict(getattr(record, "cached", {}) or {})
    failed = dict(getattr(record, "failed", {}) or {})
    cancelled = dict(getattr(record, "cancelled", {}) or {})

    sep = "═" * 90
    lines = [
        sep,
        f"● {record_file.name}",
        f"  cid={cid_str}",
        f"  requested={getattr(record, 'request_timestamp', '?')}"
        f"  author={getattr(record, 'author', '?')}"
        f"  by={getattr(record, 'author_email', '?')}",
    ]
    msg = getattr(record, "message", None)
    if msg:
        lines.append(f"  message={_truncate(msg, 200)}")
    lines.append(
        f"  completed={len(completed)} cached={len(cached)} "
        f"failed={len(failed)} cancelled={len(cancelled)}"
    )

    def block(title: str, mapping: dict, status: str, *, is_error: bool = False) -> None:
        if not mapping:
            return
        lines.append(f"  ── {title} ({len(mapping)}) ──")
        for task_cid, val in mapping.items():
            if is_error:
                # failed/cancelled map task_cid -> message string (not a run cid)
                lines.append(
                    render_task(
                        core,
                        loader,
                        task_cid,
                        "—",
                        status,
                        depth=depth,
                        max_len=max_len,
                        max_items=max_items,
                    )
                )
                lines.append(f"      reason = {_truncate(str(val), max_len)}")
            else:
                lines.append(
                    render_task(
                        core,
                        loader,
                        task_cid,
                        val,
                        status,
                        depth=depth,
                        max_len=max_len,
                        max_items=max_items,
                    )
                )

    block("completed (freshly run)", completed, "completed")
    if show_cached:
        block("cached (cache hit)", cached, "cached")
    block("failed", failed, "FAILED", is_error=True)
    block("cancelled", cancelled, "cancelled", is_error=True)
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------------
# Main / watching
# --------------------------------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("config", type=Path, help="Path to the fxtr config TOML.")
    parser.add_argument(
        "--materializations-dir",
        type=Path,
        default=None,
        help="Override the materializations dir (default: <data.active>/state/materializations).",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=1,
        help="How deep to expand argument values (0 = just type/fn names). Default 1.",
    )
    parser.add_argument(
        "--max-value-len",
        type=int,
        default=160,
        help="Truncate any single rendered value to this many chars. Default 160.",
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=8,
        help="Max kwargs / container items to show per value. Default 8.",
    )
    parser.add_argument(
        "--show-cached",
        action="store_true",
        help="Also show tasks that were cache hits (by default only freshly-run tasks are shown).",
    )
    parser.add_argument(
        "--no-catchup",
        action="store_true",
        help="Skip records already present at startup; only show ones that arrive afterwards.",
    )
    parser.add_argument(
        "--poll",
        action="store_true",
        help="Use a polling watcher (more reliable on network/remote filesystems).",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="Polling interval in seconds when --poll is set. Default 1.0.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    if not args.config.exists():
        print(f"Config not found: {args.config}", file=sys.stderr)
        return 2

    # Imported here so --help works without the (git-pulled) fxtr install resolving first.
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer
    from watchdog.observers.polling import PollingObserver

    from fxtr.api import api_lib, fxtrconfig
    from fxtr.object_model import core, locator

    config = fxtrconfig.parse_fxtr_config(args.config)
    mat_dir = args.materializations_dir or (
        fxtrconfig.get_active_state_store(config) / "materializations"
    )
    mat_dir.mkdir(parents=True, exist_ok=True)

    loader = locator.CachingEntityLoader.from_locator(api_lib.get_active_data_locator(config))

    print(f"Config:           {args.config}")
    print(f"Materializations: {mat_dir}")
    print(f"Watching for new materialization records (Ctrl-C to stop)…\n")

    def render(path: Path) -> None:
        try:
            print(
                render_record(
                    core,
                    loader,
                    path,
                    depth=args.depth,
                    max_len=args.max_value_len,
                    max_items=args.max_items,
                    show_cached=args.show_cached,
                ),
                flush=True,
            )
        except FileNotFoundError:
            pass
        except json.JSONDecodeError:
            # File may still be mid-write; the modified-event retry will pick it up.
            pass
        except Exception:
            logger.exception(f"Failed to render record {path.name}")

    seen: set[str] = set()

    # Catch-up: render existing records in timestamp order (filenames are RFC3339 timestamps).
    if not args.no_catchup:
        for path in sorted(mat_dir.glob("*.json")):
            seen.add(path.name)
            render(path)
    else:
        seen.update(p.name for p in mat_dir.glob("*.json"))

    # Hand new files off to the main thread via a queue so printing stays ordered & single-threaded.
    events: "queue.Queue[Path]" = queue.Queue()

    class Handler(FileSystemEventHandler):
        def _maybe(self, event: FileSystemEvent) -> None:
            if event.is_directory:
                return
            p = Path(str(event.src_path))
            if p.suffix == ".json":
                events.put(p)

        on_created = _maybe
        on_modified = _maybe

        def on_moved(self, event: FileSystemEvent) -> None:
            dest = getattr(event, "dest_path", None)
            if dest:
                events.put(Path(str(dest)))

    observer: Any = PollingObserver(timeout=args.poll_interval) if args.poll else Observer()
    observer.schedule(Handler(), str(mat_dir), recursive=False)
    observer.start()
    try:
        while True:
            try:
                path = events.get(timeout=0.5)
            except queue.Empty:
                continue
            if path.suffix != ".json" or not path.exists():
                continue
            if path.name in seen:
                continue
            # Tiny settle delay so we read a fully-written file (writer does one json.dump).
            time.sleep(0.05)
            seen.add(path.name)
            render(path)
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        observer.stop()
        observer.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
