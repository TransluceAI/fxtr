"""Shared execution and reporting for ``fxtr run``, ``fxtr resume``, and Python launchers.

``run_job(client, workflow, inputs)`` submits through an open :class:`ProjectClient`, accepting
a workflow object or its registered name. ``wait_for_job(client, handle)`` shows the job in the
project's running viewer (:func:`show_job`), then waits and reports the status, errors, and
result; a stopped job raises :class:`JobNotFinishedError` after its status is printed. The CLI
translates that to exit status 1. Showing the job prints its link and announces the job to the
viewer, whose open pages can switch to it; when no page heard it, it opens the link in a new
browser tab (``open_browser=False``, or :data:`~fxtr.project.viewer.NO_BROWSER_ENV` in the
environment, only prints it). With no viewer running (:meth:`ProjectClient.viewer_url`) it prints
how to start one instead. Callers own the client's lifetime, using ``open_local_client`` or a
client they configure themselves. Both helpers use only the public client and handle interfaces.

Callers provide workflows, inputs, and execution options as Python arguments and own any
command-line parsing. ``launch_cli`` is a synchronous entry point that runs a local Postgres job,
manages the event loop and client, and exits with status 1 if the job stops. Async callers use
``run_job`` directly.
For a local persistent job, ``open_local_client(project)`` locates the project, imports its
modules, and connects to the database named in ``fxtr.local.toml``. The client pins the code,
checks the environment, creates the job, and runs it in this process. ``allow_staged=True``
allows staged changes to be pinned; ``verify_environment=False`` on the client skips the venv
check. Both settings are recorded on the job attempt.

``on_result(storage, result)`` is an optional async callback for custom result output, called
instead of the default array printer while the client's storage is still open. Imports of the
Postgres backend are deferred so importing this module or requesting CLI help needs no runner
extra. Scripts needing custom input loading, cache management, or client settings can use the
project client directly.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import anyio
import anyio.to_thread
from frozendict import frozendict

from fxtr.core.entities import BoundID, FxtrEntityStorage
from fxtr.entity_defns.array import Array
from fxtr.experiment.workflows import Workflow
from fxtr.project.client import (
    JobHandle,
    JobNotFinishedError,
    JobStatus,
    ProjectClient,
    ProjectClientError,
)
from fxtr.project.formatting import conflict_patterns, format_array, print_job_status
from fxtr.project.viewer import announce_job, browser_disabled, job_url, open_job

type ResultHandler = Callable[[FxtrEntityStorage, Array[Any]], Awaitable[None]]


async def _print_result(storage: FxtrEntityStorage, result: Array[Any]) -> None:
    print()
    print(format_array(result))


def open_local_client(
    project: str | Path | None = None,
    *,
    verify_environment: bool = True,
    max_concurrency: int | None = None,
) -> AbstractAsyncContextManager[ProjectClient]:
    """Open the client for a project anchor (the working directory when omitted).

    ``max_concurrency`` limits concurrent steps; ``None`` uses the runner's default.
    The runner extra is imported here. A missing dependency raises ``ProjectClientError`` with
    the extra to install; both the CLI and Python launchers use this opener.
    """
    if max_concurrency is not None and max_concurrency < 1:
        raise ValueError("max_concurrency must be positive")
    try:
        from fxtr.runner.postgres.client import LocalProjectClient
        from fxtr.runner.postgres.runner import RunLimits
    except ModuleNotFoundError as exc:
        raise ProjectClientError(
            f"the local client needs fxtr's runner extra (fxtr[runner]): {exc}"
        ) from None
    limits = RunLimits() if max_concurrency is None else RunLimits(steps=max_concurrency)
    return LocalProjectClient.from_project(
        project, verify_environment=verify_environment, limits=limits
    )


async def show_job(client: ProjectClient, job_id: str, *, open_browser: bool = True) -> None:
    """Print where to watch a job: its link in the project's running viewer, or how to start a
    viewer when none runs. When ``open_browser``, it also shows the job in the viewer: it announces
    the job, so the viewer's open pages can switch to it, and opens a new browser tab only when no
    page heard it."""
    viewer = await client.viewer_url()
    if viewer is None:
        print(
            "no viewer is running for this project: start one with `uv run fxtr view`", flush=True
        )
        return
    print(f"watch it at {job_url(viewer, job_id)}", flush=True)
    if not open_browser or browser_disabled():
        return
    if heard := await anyio.to_thread.run_sync(announce_job, viewer, job_id):
        print(f"sent it to the viewer's {_tabs(heard)}", flush=True)
    elif problem := await anyio.to_thread.run_sync(open_job, viewer, job_id):
        print(f"could not open a browser: {problem}", flush=True)


def _tabs(count: int) -> str:
    return "open tab" if count == 1 else f"{count} open tabs"


async def wait_for_job(
    client: ProjectClient,
    handle: JobHandle,
    *,
    on_result: ResultHandler = _print_result,
    open_browser: bool = True,
) -> Array[Any]:
    """Show a submitted or resumed job (:func:`show_job`), wait, report its status, and return
    its result. A stopped job raises ``JobNotFinishedError`` after reporting its errors or
    reason; its result callback is not called. The caller keeps ``client`` open for this whole
    call."""
    print(f"job {handle.job_id}: running", flush=True)
    await show_job(client, str(handle.job_id), open_browser=open_browser)
    status = await handle.wait()
    await report_job_status(client, status)
    if status.state != "succeeded":
        raise JobNotFinishedError(f"Job {handle.job_id} is {status.state}")
    result = await handle.result()
    await on_result(client, result)
    return result


async def report_job_status(client: ProjectClient, status: JobStatus) -> None:
    """Print the status of a job (see :func:`~fxtr.project.formatting.print_job_status`).

    If the job has cache conflicts, this function first lists the cache entries under the literal
    prefix of each conflict group. Each group then shows how many entries its pattern matches.

    Args:
        client: The open client of the project of the job.
        status: The status of the job.
    """
    cache_addresses: set[str] | None = None
    if status.cache_conflicts:
        cache_addresses = set()
        for prefix in {p.literal_prefix for p in conflict_patterns(status.cache_conflicts)}:
            cache_addresses.update(e.cache_address for e in await client.list_cache_entries(prefix))
    print_job_status(status, cache_addresses)


async def run_job(
    client: ProjectClient,
    workflow: Workflow[Any, Any, Any] | str,
    inputs: Mapping[str, Array[Any] | BoundID[Array[Any]]] = frozendict(),
    *,
    root: str = "root",
    allow_staged: bool = False,
    overwrite_cache_conflicts: bool = False,
    on_result: ResultHandler = _print_result,
    open_browser: bool = True,
) -> Array[Any]:
    """Submit a workflow through an open client, show it in the project's viewer, wait, and report
    its result.

    Inputs are prepared arrays or bound ids such as project-array snapshots. The client owns its
    execution settings, including environment verification. ``on_result`` runs with that client
    as its storage; the caller keeps it open. ``open_browser=False`` only prints the job's link.
    Raises ``JobNotFinishedError`` if the job stops.

    Example::

        async with open_local_client(__file__) as client:
            result = await run_job(client, workflow, inputs)

    Args:
        client: The open client.
        workflow: The root workflow, or its registered name.
        inputs: The inputs of the workflow, by name.
        root: The root pathname and default cache address prefix of the job.
        allow_staged: Whether staged changes may be pinned.
        overwrite_cache_conflicts: If ``True``, the job overwrites the cache conflicts it finds
            with completed results (see :meth:`~fxtr.project.client.ProjectClient.submit`).
        on_result: What to do with the result.
        open_browser: Whether to show the job in the project's running viewer (see
            :func:`show_job`).
    """
    handle = await client.submit(
        workflow,
        inputs,
        root=root,
        allow_staged=allow_staged,
        overwrite_cache_conflicts=overwrite_cache_conflicts,
    )
    return await wait_for_job(client, handle, on_result=on_result, open_browser=open_browser)


def launch_cli(
    workflow: Workflow[Any, Any, Any],
    inputs: Mapping[str, Array[Any]] = frozendict(),
    *,
    anchor: str | Path,
    allow_staged: bool = False,
    verify_environment: bool = True,
    max_concurrency: int | None = None,
    overwrite_cache_conflicts: bool = False,
    on_result: ResultHandler = _print_result,
    open_browser: bool = True,
) -> None:
    """Run a local Postgres job with explicit options; the caller owns argument parsing.

    ``anchor`` identifies the project, usually the launcher's ``__file__``. This synchronous
    entry point owns the event loop and the local client's lifetime. A stopped job is reported
    and exits with status 1. ``on_result`` customizes result output while the client is open.
    ``max_concurrency`` limits concurrent steps; ``None`` uses the runner's default.
    ``open_browser=False`` only prints the job's link instead of also showing it in the viewer.
    ``overwrite_cache_conflicts`` is as for :func:`run_job`. Async callers use ``run_job``
    directly.
    """

    async def launch_job() -> None:
        async with open_local_client(
            anchor, verify_environment=verify_environment, max_concurrency=max_concurrency
        ) as client:
            await run_job(
                client,
                workflow,
                inputs,
                allow_staged=allow_staged,
                overwrite_cache_conflicts=overwrite_cache_conflicts,
                on_result=on_result,
                open_browser=open_browser,
            )

    try:
        anyio.run(launch_job)
    except JobNotFinishedError:
        raise SystemExit(1) from None
