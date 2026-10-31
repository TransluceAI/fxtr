"""The ``fxtr`` command line: a small user of :class:`~fxtr.project.client.ProjectClient`.

Every job, cache, and ``init`` command opens the client of the project the working directory (or
``--project``) belongs to and does one thing through the client's interface: list and inspect
jobs, run and resume them, cancel them, export and import their records, list, clear, and adopt
into the step cache. It prints what that interface says and nothing more, so what the command line
shows is what any client, local or remote, could show; richer inspection belongs to the viewer,
and richer launches (inputs beyond scalars and project arrays) to launcher scripts. Four commands
open no client: ``new`` writes a project from the template (:mod:`fxtr.project.new`), ``views
vendor`` copies in the fxtr packages its renderers use, ``views build`` builds its renderers, and
``view`` serves it to the viewer.

The client is the local one (:class:`~fxtr.runner.postgres.client.LocalProjectClient`), imported
only when a command needs it, so ``fxtr --help`` works without the runner extra. A command that
runs a job runs it in this process and returns when the job stops, as a launcher script's
``client.run`` does; Ctrl-C is the client's (soft cancel, then hard, then stop).

``run`` and ``resume`` use :mod:`fxtr.project.running` for submission, waiting, and result
reporting, as Python launchers do. This module resolves command-line inputs and translates
exceptions into exit codes. The local-client opener also lives in ``running``; argument parsing
stays here, and job status formatting lives in :mod:`fxtr.project.formatting`.

Output: results on stdout; the runner's progress (a line per attempt start and end, from the
``fxtr`` loggers) and refusals on stderr. A refusal the user can act on (no project here, a
malformed config, a checkout that cannot be pinned, an unknown job) prints its message without a
traceback. Exit status: 0 when the command did what it was asked and any job it ran succeeded; 1
when a job it ran stopped without succeeding; 2 for a refusal or a usage error.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any, Protocol

import anyio

from fxtr.core.entities import BoundID
from fxtr.entity_defns.array import Array
from fxtr.experiment.registry import RegistryError
from fxtr.experiment.signatures import SignatureError
from fxtr.project.client import (
    AdoptReport,
    JobID,
    JobNotFinishedError,
    ProjectClient,
    ProjectClientError,
)
from fxtr.project.config import (
    LOCAL_CONFIG_FILENAME,
    ProjectError,
    find_project_root,
    load_views,
    write_local_config,
)
from fxtr.project.environment import PinRefusedError
from fxtr.project.formatting import format_adopt_report
from fxtr.project.running import open_local_client, report_job_status, run_job, wait_for_job
from fxtr.runner.cache_addresses import PatternError


class OpenClient(Protocol):
    """How a command gets its client, to enter: for the project ``project`` is in (the working
    directory when ``None``), with the environment check on or off for its launches. The local
    client's ``from_project`` by default; tests substitute their own."""

    def __call__(
        self, project: Path | None, *, verify_environment: bool
    ) -> AbstractAsyncContextManager[ProjectClient]: ...


EXIT_OK = 0
EXIT_STOPPED = 1
EXIT_REFUSED = 2

_REFUSALS = (ProjectError, ProjectClientError, RegistryError, SignatureError, PatternError)
"""What a command reports as a message rather than a traceback: the user can act on these."""


class CommandError(Exception):
    """A refusal of the command line's own: an argument argparse cannot check (a malformed job id
    or input), or a missing extra."""


async def main(argv: Sequence[str], *, open_client: OpenClient | None = None) -> int:
    """Run the command line on ``argv`` and return the exit status."""
    args = _parse(argv)
    _configure_logging(args.verbosity)
    opener = open_local_client if open_client is None else open_client
    try:
        return await args.command(opener, args)
    except JobNotFinishedError:
        # The shared launch reporter has printed the stopped job's errors or reason.
        return EXIT_STOPPED
    except (CommandError, *_REFUSALS) as exc:
        print(f"fxtr: {_message(exc)}", file=sys.stderr)
        return EXIT_REFUSED


def entry() -> None:
    """The ``fxtr`` script."""
    sys.exit(anyio.run(main, sys.argv[1:]))


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    args = _parser().parse_args(argv)
    # The shared options are accepted before or after a subcommand, so neither level defaults
    # them (a subcommand's default would override what the top level parsed).
    args.project = getattr(args, "project", None)
    args.verbosity = getattr(args, "verbosity", 0)
    return args


# ---- the commands ----


async def _run(open_client: OpenClient, args: argparse.Namespace) -> int:
    given = [_input(text) for text in args.input]
    async with _open(open_client, args) as client:
        inputs: dict[str, Array[Any] | BoundID[Array[Any]]] = {}
        for name, value in given:
            if isinstance(value, _ProjectArray):
                inputs[name] = (await client.snapshot_array(value.name)).array
            else:
                inputs[name] = value
        await run_job(
            client,
            args.workflow,
            inputs,
            root=args.root,
            allow_staged=args.allow_staged,
            open_browser=not args.no_open,
            overwrite_cache_conflicts=args.overwrite_cache_conflicts,
        )
    return EXIT_OK


async def _resume(open_client: OpenClient, args: argparse.Namespace) -> int:
    job_id = _job_id(args.job)
    async with _open(open_client, args) as client:
        handle = await client.resume(
            job_id,
            allow_staged=args.allow_staged,
            overwrite_cache_conflicts=args.overwrite_cache_conflicts,
        )
        await wait_for_job(client, handle, open_browser=not args.no_open)
    return EXIT_OK


async def _jobs_cancel(open_client: OpenClient, args: argparse.Namespace) -> int:
    job_id = _job_id(args.job)
    async with _open(open_client, args) as client:
        handle = await client.job(job_id)
        state = (await handle.status()).state
        if state != "running":
            print(f"job {job_id}: {state}, nothing to cancel")
            return EXIT_OK
        await handle.cancel(hard=args.hard)
        kind = "hard cancel" if args.hard else "cancel"
        print(f"job {job_id}: {kind} requested")
    return EXIT_OK


async def _jobs_export(open_client: OpenClient, args: argparse.Namespace) -> int:
    job_id = _job_id(args.job)
    async with _open(open_client, args) as client:
        record = await client.export_job(job_id, args.path)
    print(f"job {job_id}: exported to {args.path} (record {record.cid_str()})")
    return EXIT_OK


async def _jobs_import(open_client: OpenClient, args: argparse.Namespace) -> int:
    if args.overwrite_cache_conflicts and not args.adopt_cache:
        raise CommandError("--overwrite-cache-conflicts needs --adopt-cache")
    async with _open(open_client, args) as client:
        job_id = await client.import_job(args.path, update=args.update)
        print(f"job {job_id}: imported from {args.path}")
        if args.adopt_cache:
            report = await client.adopt_cache(
                job_id, overwrite_cache_conflicts=args.overwrite_cache_conflicts
            )
            _print_adopt_report(job_id, report)
    return EXIT_OK


async def _cache_list(open_client: OpenClient, args: argparse.Namespace) -> int:
    conflicts_in = None if args.conflicts_in is None else _job_id(args.conflicts_in)
    async with _open(open_client, args) as client:
        entries = await client.list_cache_entries(
            args.prefix, step=args.step, state=args.state, conflicts_in=conflicts_in
        )
        for entry in entries:
            print(f"{entry.state:<9}  {entry.cache_address}  ({entry.step_name})")
    return EXIT_OK


async def _cache_clear(open_client: OpenClient, args: argparse.Namespace) -> int:
    chosen = [args.prefix is not None, bool(args.address), args.conflicts_in is not None, args.all]
    if sum(chosen) != 1:
        raise CommandError(
            "give exactly one of a PATTERN, --address (repeatable), --conflicts-in, and --all"
        )
    prefix = "" if args.all else args.prefix
    conflicts_in = None if args.conflicts_in is None else _job_id(args.conflicts_in)
    async with _open(open_client, args) as client:
        running: list[str] = []
        if conflicts_in is not None:
            status = await (await client.job(conflicts_in)).status()
            running = [c.cache_address for c in status.cache_conflicts if c.kind == "running"]
        cleared = await client.clear_cache_entries(
            prefix,
            addresses=args.address or None,
            conflicts_in=conflicts_in,
            step=args.step,
            state=args.state,
        )
    print(f"cleared {cleared} cache {'entry' if cleared == 1 else 'entries'}")
    if running:
        print(
            f"left {len(running)} {'conflict' if len(running) == 1 else 'conflicts'} with a"
            " running attempt of another job; wait for it to finish and clear again, or cancel"
            " that job:"
        )
        for cache_address in running:
            print(f"  {cache_address}")
    return EXIT_OK


async def _cache_adopt(open_client: OpenClient, args: argparse.Namespace) -> int:
    job_id = _job_id(args.job)
    async with _open(open_client, args) as client:
        report = await client.adopt_cache(
            job_id, args.prefix, overwrite_cache_conflicts=args.overwrite_cache_conflicts
        )
    _print_adopt_report(job_id, report)
    return EXIT_OK


def _print_adopt_report(job_id: JobID, report: AdoptReport) -> None:
    first, *rest = format_adopt_report(report)
    print(f"job {job_id}: {first}")
    for line in rest:
        print(f"  {line}")


async def _init(open_client: OpenClient, args: argparse.Namespace) -> int:
    root = find_project_root(Path.cwd() if args.project is None else args.project)
    if (root / LOCAL_CONFIG_FILENAME).exists() and not args.force:
        raise CommandError(
            f"{root / LOCAL_CONFIG_FILENAME} exists already; pass --force to replace it"
        )
    path = write_local_config(root, database_url=args.database_url, schema=args.schema)
    # Opening the client creates the project's tables in its database if they are missing.
    async with _open(open_client, args):
        pass
    print(f"wrote {path}; the project's database is ready")
    return EXIT_OK


async def _new(open_client: OpenClient, args: argparse.Namespace) -> int:
    from fxtr.project import new

    project = new.write_project(
        args.directory,
        checkout=new.find_checkout(),
        name=args.name,
        example=args.example,
    )
    root = project.root
    print(f"wrote the fxtr project {project.name} in {root}", flush=True)
    if not new.in_repository(root):
        await new.init_git(root)
        print("  initialized a git repository there", flush=True)
    steps: list[tuple[str, str]] = [("make its venv and uv.lock", "uv sync")]
    steps.append(
        (
            "say where the project's database is",
            "uv run fxtr init --database-url postgresql+asyncpg://fxtr:fxtr@localhost:55433/fxtr",
        )
    )
    install = f"(cd {new.VIEWS_DIR} && pnpm install)"
    steps.append(("build its renderers", f"{install} && uv run fxtr views build"))
    steps.append(
        (
            "commit: a launch pins the checkout's commit",
            "git add . && git commit -m 'A new fxtr project'",
        )
    )
    if project.example:
        steps += [
            ("run the example experiment as a job", "uv run python launch.py"),
            (
                "run the example workflow as a job",
                f"uv run fxtr run {new.WORKFLOW_NAME} --input salutation=Hello",
            ),
        ]
    print(f"\nnext, in {root} (its README.md says more):")
    for why, command in steps:
        print(f"  # {why}\n  {command}")
    if not project.example:
        print(
            f"\nThis project has no experiments yet. Add your steps and workflows in"
            f" src/{project.package}/ and register their modules in [tool.fxtr] modules."
            "\nSee https://docs.transluce.ai/fxtr/first-experiment for a walkthrough."
        )
    return EXIT_OK


async def _views_vendor(open_client: OpenClient, args: argparse.Namespace) -> int:
    from fxtr.project.views import vendor_packages

    root = find_project_root(Path.cwd() if args.project is None else args.project)
    views = load_views(root)
    if views is None:
        raise CommandError(f"{root} configures no view bundle: add a [tool.fxtr.views] table")
    vendored = vendor_packages(views.package)
    print(f"copied fxtr {vendored.fxtr_version}'s TypeScript packages to {vendored.directory}")
    if vendored.missing_dependencies:
        needed = ", ".join(f"{name}@{spec}" for name, spec in vendored.missing_dependencies.items())
        print(f"add the packages they need to {views.package / 'package.json'}: {needed}")
    print(f"next: `pnpm install` in {views.package}, then `fxtr views build`")
    return EXIT_OK


async def _views_build(open_client: OpenClient, args: argparse.Namespace) -> int:
    from fxtr.project.views import build_views

    root = find_project_root(Path.cwd() if args.project is None else args.project)
    views = load_views(root)
    if views is None:
        raise CommandError(f"{root} configures no view bundle: add a [tool.fxtr.views] table")
    manifest = (await build_views(views)).manifest
    files = ", ".join(f for f in (manifest.module, manifest.stylesheet) if f is not None)
    print(f"built the view bundle in {manifest.dist} ({files})")
    return EXIT_OK


async def _view(open_client: OpenClient, args: argparse.Namespace) -> int:
    try:
        from fxtr.runner.postgres.debug_web.ports import ViewerPortError
        from fxtr.runner.postgres.debug_web.server import serve
    except ModuleNotFoundError as exc:
        raise CommandError(
            f"the viewer needs fxtr's debug-web extra (fxtr[debug-web]): {exc}"
        ) from None
    try:
        await serve(
            args.project,
            host=args.host,
            port=args.port,
            open_browser=not args.no_open,
        )
    except ViewerPortError as exc:
        raise CommandError(str(exc)) from None
    return EXIT_OK


async def _jobs_list(open_client: OpenClient, args: argparse.Namespace) -> int:
    async with _open(open_client, args) as client:
        for job in await client.list_jobs():
            created = job.created_at.isoformat(sep=" ", timespec="seconds")
            print(f"{job.job_id}  {job.state:<9}  {created}  {job.workflow_name} ({job.root})")
    return EXIT_OK


async def _jobs_status(open_client: OpenClient, args: argparse.Namespace) -> int:
    job_id = _job_id(args.job)
    async with _open(open_client, args) as client:
        handle = await client.job(job_id)
        await report_job_status(client, await handle.status())
    return EXIT_OK


# ---- plumbing ----


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--project",
        type=Path,
        default=argparse.SUPPRESS,
        help="a path inside the project (the working directory by default)",
    )
    verbosity = common.add_mutually_exclusive_group()
    for flags, level, help in (
        (("-q", "--quiet"), -1, "show only warnings from the runner"),
        (("-v", "--verbose"), 1, "show the runner's debug output"),
    ):
        verbosity.add_argument(
            *flags,
            dest="verbosity",
            action="store_const",
            const=level,
            default=argparse.SUPPRESS,
            help=help,
        )

    parser = argparse.ArgumentParser(prog="fxtr", parents=[common])
    commands = parser.add_subparsers(required=True, metavar="command")

    launch = argparse.ArgumentParser(add_help=False)
    launch.add_argument(
        "--allow-staged",
        action="store_true",
        help="pin staged but uncommitted changes as a commit of fxtr's own",
    )
    launch.add_argument(
        "--no-verify", action="store_true", help="skip the check that the venv matches the project"
    )
    launch.add_argument(
        "--no-open",
        action="store_true",
        help="only print the job's link in the project's running viewer, without showing it in the"
        " viewer's open tabs or a new one",
    )
    launch.add_argument(
        "--overwrite-cache-conflicts",
        action="store_true",
        help="run the steps whose cache addresses hold a different request's completed result, and"
        " replace those results, here and at the steps after them (for this run only; jobs that"
        " took the old results keep them)",
    )

    run = commands.add_parser(
        "run",
        parents=[common, launch],
        help="run a workflow as a job, here, until it stops",
        description="Run a workflow as a job of the project, in this process, until it stops.",
    )
    run.add_argument(
        "workflow",
        help="its registered name (the project's [tool.fxtr] modules are imported first, which is"
        " also what lets a resume find it)",
    )
    run.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="an input: a JSON scalar (a string need not be quoted), or @ARRAY for a snapshot of"
        " the project array ARRAY; repeat for each input. Anything richer takes a launcher script.",
    )
    run.add_argument("--root", default="root", help="the root invocation's pathname")
    run.set_defaults(command=_run)

    resume = commands.add_parser(
        "resume",
        parents=[common, launch],
        help="run a stopped job's unfinished work, here, until it stops",
    )
    resume.add_argument("job", help="the job's id")
    resume.set_defaults(command=_resume)

    jobs = commands.add_parser(
        "jobs", parents=[common], help="list, inspect, cancel, export, and import jobs"
    )
    jobs_commands = jobs.add_subparsers(required=True, metavar="command")
    jobs_commands.add_parser("list", parents=[common], help="every job, newest first").set_defaults(
        command=_jobs_list
    )
    status = jobs_commands.add_parser("status", parents=[common], help="where a job is")
    status.add_argument("job", help="the job's id")
    status.set_defaults(command=_jobs_status)
    cancel = jobs_commands.add_parser(
        "cancel",
        parents=[common],
        help="ask the process running a job to stop it",
        description="Ask the process running a job to stop it: a soft cancel lets running"
        " attempts finish and starts nothing new; --hard stops them too. The job can be resumed.",
    )
    cancel.add_argument("job", help="the job's id")
    cancel.add_argument("--hard", action="store_true", help="stop running attempts too")
    cancel.set_defaults(command=_jobs_cancel)
    export = jobs_commands.add_parser(
        "export",
        parents=[common],
        help="write a job's record to a file",
        description="Write a stopped or succeeded job's record (its facts, and every entity they"
        " reach) to a file, as a gzipped CAR another project can import.",
    )
    export.add_argument("job", help="the job's id")
    export.add_argument("path", type=Path, help="the file to write")
    export.set_defaults(command=_jobs_export)
    import_ = jobs_commands.add_parser(
        "import",
        parents=[common],
        help="add a job from a record file",
        description="Add the job a record file (from `fxtr jobs export`) holds to this project,"
        " with everything it reaches. The commits its results were produced at must be in this"
        " repository, and the job must not be here already unless --update is given. The step"
        " cache is left alone unless --adopt-cache is given.",
    )
    import_.add_argument("path", type=Path, help="the record file")
    import_.add_argument(
        "--update",
        action="store_true",
        help="bring a job this project has up to date from a later record of it; the record must"
        " hold everything this project knows of the job",
    )
    import_.add_argument(
        "--adopt-cache",
        action="store_true",
        help="then make the step cache offer the job's results (as `fxtr cache adopt` does)",
    )
    import_.add_argument(
        "--overwrite-cache-conflicts",
        action="store_true",
        help="with --adopt-cache, replace entries that hold a different request's result",
    )
    import_.set_defaults(command=_jobs_import)

    cache = commands.add_parser(
        "cache", parents=[common], help="list, clear, and adopt into the step cache"
    )
    cache_commands = cache.add_subparsers(required=True, metavar="command")
    selection = argparse.ArgumentParser(add_help=False)
    selection.add_argument("--step", help="only entries computed by the step of this name")
    selection.add_argument(
        "--state",
        choices=("completed", "running", "failed", "abandoned"),
        help="only entries in this state",
    )
    cache_list = cache_commands.add_parser(
        "list", parents=[common, selection], help="the cache's entries, oldest first"
    )
    cache_list.add_argument(
        "prefix",
        nargs="?",
        metavar="PATTERN",
        help='only addresses at or under this one: an address prefix (such as "root/refine"), in'
        ' which a mapped value can be * (such as "root/refine[bio:*]")',
    )
    cache_list.add_argument(
        "--conflicts-in",
        metavar="JOB",
        help="only the entries this job is in cache conflict with",
    )
    cache_list.set_defaults(command=_cache_list)
    cache_clear = cache_commands.add_parser(
        "clear",
        parents=[common, selection],
        help="forget entries, so the next job to request them runs the steps afresh",
        description="Forget cache entries, so the next job to request them runs the steps"
        " afresh. Jobs already bound to a result keep it.",
    )
    cache_clear.add_argument(
        "prefix",
        nargs="?",
        metavar="PATTERN",
        help="the addresses at or under this one: an address prefix, in which a mapped value can"
        " be *",
    )
    cache_clear.add_argument(
        "--address", action="append", default=[], help="exactly this address (repeatable)"
    )
    cache_clear.add_argument(
        "--conflicts-in",
        metavar="JOB",
        help="the completed entries this job is in cache conflict with now (its steps after"
        " them can conflict again when it resumes)",
    )
    cache_clear.add_argument("--all", action="store_true", help="every entry")
    cache_clear.set_defaults(command=_cache_clear)
    cache_adopt = cache_commands.add_parser(
        "adopt",
        parents=[common],
        help="make the cache offer the results a job took, so later jobs reuse them",
        description="Make the step cache offer the step results a job took, local or imported,"
        " so that later jobs of the same requests reuse them. An address where an attempt runs, or"
        " where the same request's result is already, is left alone; an address where a different"
        " request's result is, is left alone unless --overwrite-cache-conflicts is given.",
    )
    cache_adopt.add_argument("job", help="the job's id")
    cache_adopt.add_argument(
        "prefix",
        nargs="?",
        metavar="PATTERN",
        help="only addresses at or under this one: an address prefix, in which a mapped value can"
        " be *",
    )
    cache_adopt.add_argument(
        "--overwrite-cache-conflicts",
        action="store_true",
        help="replace entries that hold a different request's result",
    )
    cache_adopt.set_defaults(command=_cache_adopt)

    new = commands.add_parser(
        "new",
        help="write an empty project, or include a demo with --example",
        description="Write a new fxtr project in DIRECTORY: its pyproject.toml, an empty Python"
        " package, a .gitignore, a README, and an empty renderer package."
        " Pass --example to include a greeting experiment, launcher, and example renderers."
        " The project depends on this fxtr's checkout by path, or, for a fxtr installed from PyPI,"
        " on fxtr from PyPI at this version or later; then make it a git repository (unless it is"
        " in one) and print the setup commands. Inside the fxtr checkout the project refers to it"
        " by relative paths. Outside it, the renderer package gets a copy of the fxtr TypeScript"
        " packages (fxtr views vendor).",
    )
    new.add_argument("directory", type=Path, help="where: a new or empty directory")
    new.add_argument("--name", help="the project's name (the directory's name by default)")
    new.add_argument(
        "--example", action="store_true", help="include a runnable greeting experiment and launcher"
    )
    new.set_defaults(command=_new)

    init = commands.add_parser(
        "init",
        parents=[common],
        help="say where this machine reaches the project's database",
        description=f"Write the project's {LOCAL_CONFIG_FILENAME} (local; not committed) and"
        " create the project's tables in that database. The project's pyproject.toml must"
        " already have a [tool.fxtr] table.",
    )
    init.add_argument("--database-url", required=True, help="a SQLAlchemy URL")
    init.add_argument("--schema", help="the Postgres schema (fxtr_<project name> by default)")
    init.add_argument("--force", action="store_true", help="replace an existing file")
    init.set_defaults(command=_init)

    views = commands.add_parser("views", parents=[common], help="the project's view bundle")
    views_commands = views.add_subparsers(required=True, metavar="command")
    views_commands.add_parser(
        "build",
        parents=[common],
        help="build the project's renderer package into its view bundle",
        description="Build the project's renderer package ([tool.fxtr.views] package) into its view"
        " bundle (the package's dist/) with the package's build script, check the result, and"
        " only then replace the previous bundle.",
    ).set_defaults(command=_views_build)
    views_commands.add_parser(
        "vendor",
        parents=[common],
        help="copy this fxtr's TypeScript packages into the project's renderer package",
        description="Copy the fxtr TypeScript packages a renderer package depends on (fxtr-view,"
        " fxtr-view-kit, fxtr-view-bundle, ...) from this fxtr into the project's renderer package,"
        " at vendor/fxtr/, replacing any copy there; the package links them from there. Run it"
        " after upgrading fxtr, then `pnpm install` in the package. Commit the copy with the"
        " project. A renderer package inside the fxtr checkout uses the checkout's packages"
        " instead.",
    ).set_defaults(command=_views_vendor)

    view = commands.add_parser(
        "view",
        parents=[common],
        help="serve the project's jobs to the experiment viewer",
        description="Serve the experiment viewer and the project's jobs, on one port, until"
        " stopped. Prints the viewer's URL and opens it in a browser once listening. From a"
        " checkout of fxtr, the viewer is its Vite dev server (with hot reloading), run alongside."
        " First rebuilds the project's view bundle if its sources changed since it was built; a"
        " failed build keeps the last valid bundle, or the default renderers, and watches for"
        " source fixes. A checkout whose dev server cannot start falls"
        " back to its built viewer, rebuilding it if its sources changed and refusing to serve"
        " it if the build fails.",
    )
    view.add_argument("--host", default="127.0.0.1")
    view.add_argument(
        "--port",
        type=int,
        help="require this port; otherwise prefer the last successful port, then try 8000–8050",
    )
    view.add_argument("--no-open", action="store_true", help="do not open a browser")
    view.set_defaults(command=_view)
    return parser


def _configure_logging(verbosity: int) -> None:
    """Show the ``fxtr`` loggers on stderr at INFO (``-q``: WARNING; ``-v``: DEBUG), and other
    libraries' only from WARNING."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s", stream=sys.stderr)
    level = {-1: logging.WARNING, 0: logging.INFO, 1: logging.DEBUG}[verbosity]
    logging.getLogger("fxtr").setLevel(level)


def _open(
    open_client: OpenClient, args: argparse.Namespace
) -> AbstractAsyncContextManager[ProjectClient]:
    return open_client(args.project, verify_environment=not getattr(args, "no_verify", False))


class _ProjectArray:
    """An ``@NAME`` input: a snapshot of the project array ``name``, taken at launch."""

    def __init__(self, name: str) -> None:
        self.name = name


def _input(text: str) -> tuple[str, Array[Any] | _ProjectArray]:
    """One ``--input NAME=VALUE``: ``@ARRAY`` names a project array; otherwise the value is a JSON
    scalar (a string, number, or boolean), and text that is not JSON is a string."""
    name, sep, value = text.partition("=")
    if not sep or not name:
        raise CommandError(f"an input is NAME=VALUE, not {text!r}")
    if value.startswith("@"):
        return name, _ProjectArray(value[1:])
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = value
    if not isinstance(parsed, (str, int, float, bool)):
        raise CommandError(
            f"input {name!r} is not a scalar or a project array. To pass richer inputs, write a"
            " launcher script against the project client and run that instead of `fxtr run`."
        )
    return name, Array.infer_scalar(parsed)


def _job_id(text: str) -> JobID:
    try:
        return JobID(uuid.UUID(text))
    except ValueError:
        raise CommandError(f"not a job id: {text!r}") from None


def _message(exc: BaseException) -> str:
    # A KeyError's str() is the repr of its argument; the message is the argument itself.
    message = str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)
    note = _command_line_note(exc)
    return message if note is None else f"{message}\n{note}"


def _command_line_note(exc: BaseException) -> str | None:
    """How to do on the command line what a refusal's message says in Python terms."""
    if isinstance(exc, PinRefusedError):
        return "(On the command line, allow_staged=True is --allow-staged.)"
    try:
        from fxtr.runner.postgres.client import EnvironmentNotSyncedError
    except ModuleNotFoundError:
        return None
    if isinstance(exc, EnvironmentNotSyncedError):
        return "(On the command line, verify_environment=False is --no-verify.)"
    return None
