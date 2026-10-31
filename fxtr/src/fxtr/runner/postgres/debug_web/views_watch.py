"""The current renderer bundle and its filesystem watcher.

A published version names immutable bytes. Only the current snapshot is kept; publishing a
successful build replaces it, and failed builds leave it alone. HTTP requests for another
version must fail so the browser can restart loading from the current manifest.

``run`` establishes watching before its initial refresh and signals task readiness once that
refresh finishes, including when the build fails.
Every refresh reconciles the captured build's source fingerprint after building, so edits made
during a build cause another build. Re-establishing watch roots also triggers reconciliation.
The native watch remains active while Python builds; its next batch includes intervening edits.
Build failures retain the last valid bundle, including one found at startup, and report why.
Without a valid bundle, the viewer uses its default renderers until a build succeeds.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal

import anyio
import anyio.to_thread
import watchfiles
from anyio.abc import TaskStatus

from fxtr.project.config import ViewsConfig
from fxtr.project.views import (
    ViewBundle,
    ViewsBuildError,
    ViewsError,
    build_views,
    bundle_dir,
    is_source_path,
    read_bundle,
    source_packages,
)

DEBOUNCE_MS = 250


@dataclasses.dataclass(frozen=True)
class ViewsState:
    """The published version, build progress, and error."""

    version: str | None
    state: Literal["ready", "building", "failed"]
    error: str | None = None

    def to_json(self) -> dict[str, str | None]:
        return {
            "version": self.version,
            "state": self.state,
            "error": self.error,
        }


class ViewsWatcher:
    """Owns the current snapshot and state, keeping it built with ``run``.

    Snapshot publication and state changes happen on the event loop. Disk reads and hashing
    happen in worker threads; responses read only already-captured bytes.
    """

    def __init__(self, views: ViewsConfig, *, debounce_ms: int = DEBOUNCE_MS) -> None:
        self.views = views
        self.debounce_ms = debounce_ms
        self._bundle: ViewBundle | None = None
        self._state = ViewsState(version=None, state="building")
        self._changed = anyio.Event()
        self._refresh_lock = anyio.Lock()

    @property
    def state(self) -> ViewsState:
        return self._state

    def bundle(self, version: str | None = None) -> ViewBundle | None:
        """The current build, provided it matches any requested version."""
        if version is not None and version != self.state.version:
            return None
        return self._bundle

    def _publish(self, state: ViewsState) -> None:
        if state == self._state:
            return
        self._state = state
        changed, self._changed = self._changed, anyio.Event()
        changed.set()

    async def updates(self, *, timeout: float) -> AsyncIterator[ViewsState | None]:
        """The current state, then changes; ``None`` after ``timeout`` seconds of quiet."""
        shown: ViewsState | None = None
        while True:
            changed = self._changed
            if self._state != shown:
                shown = self._state
                yield shown
                continue
            with anyio.move_on_after(timeout) as scope:
                await changed.wait()
            if scope.cancelled_caught:
                yield None

    async def refresh(self) -> None:
        """Build until the captured inputs are current; publish only complete snapshots."""
        async with self._refresh_lock:
            try:
                bundle: ViewBundle | None = None
                try:
                    bundle = await anyio.to_thread.run_sync(read_bundle, bundle_dir(self.views))
                except ViewsError as exc:
                    reason = str(exc)
                else:
                    if self.bundle() is None:
                        self._bundle = bundle
                        self._publish(ViewsState(version=bundle.version, state="building"))
                    reason = await anyio.to_thread.run_sync(bundle.stale_reason, self.views)
                while bundle is None or reason is not None:
                    self._publish(ViewsState(version=self.state.version, state="building"))
                    print(f"building the project's view bundle ({reason})", flush=True)
                    bundle = await build_views(self.views)
                    print(f"built the view bundle in {bundle.manifest.dist}", flush=True)
                    reason = await anyio.to_thread.run_sync(bundle.stale_reason, self.views)
                self._bundle = bundle
                self._publish(ViewsState(version=bundle.version, state="ready"))
            except Exception as exc:  # noqa: BLE001 - the viewer outlives a broken refresh
                error = str(exc)
                if isinstance(exc, ViewsBuildError) and exc.output:
                    error = f"{error}\n\n{exc.output}"
                kept = "serving the previous one" if self.bundle() is not None else "serving none"
                print(f"could not build the project's view bundle: {error}; {kept}", flush=True)
                self._publish(ViewsState(version=self.state.version, state="failed", error=error))

    async def run(self, *, task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED) -> None:
        """Watch before the initial build; signal readiness after the first build attempt."""
        started = False
        while True:
            roots = await anyio.to_thread.run_sync(self._roots)
            reconcile = True
            async for changes in watchfiles.awatch(
                *roots,
                watch_filter=self._filter(roots),
                debounce=self.debounce_ms,
                step=50,
                rust_timeout=100,
                yield_on_timeout=True,
            ):
                # The first yield (even an empty timeout) means the native watch is installed.
                if reconcile or changes:
                    await self.refresh()
                    reconcile = False
                    if not started:
                        task_status.started()
                        started = True
                    if await anyio.to_thread.run_sync(self._roots) != roots:
                        break

    def _roots(self) -> list[Path]:
        # Watching an existing parent also notices a missing package being created.
        roots = []
        for package in source_packages(self.views.package):
            root = package
            while not root.is_dir():
                root = root.parent
            roots.append(root)
        return sorted(set(roots))

    @staticmethod
    def _filter(roots: list[Path]) -> watchfiles.BaseFilter:
        class SourcesFilter(watchfiles.BaseFilter):
            def __call__(self, change: watchfiles.Change, path: str) -> bool:
                resolved = Path(path)
                return any(
                    resolved.is_relative_to(root) and is_source_path(root, resolved)
                    for root in roots
                )

        return SourcesFilter()
