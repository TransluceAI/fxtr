"""File list controller for clydra - tracks files in cwd for @-mention completion."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

from thingwatcher import ThingRef, ThingState
from thingwatcher.api import AnchorCapturer, StatefulOwningSnapshotter

# The key for the file list thing
FILE_LIST_KEY = ["clydra_file_list"]


def scan_files(cwd: str) -> list[str]:
    """Scan files using fd. Respects gitignore automatically."""
    try:
        result = subprocess.run(
            ["fd", "--type", "f"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return [f for f in result.stdout.strip().split("\n") if f]
    except Exception:
        pass
    return []


@dataclass
class FileListController:
    """Controller for file list tracking.

    This is a tracked thing that provides the list of files in cwd
    for @-mention autocompletion in the input box.

    Attributes:
        cwd: The working directory to scan.
        files: The current list of files (relative paths).
    """

    cwd: str
    files: list[str] = field(default_factory=list)

    def rescan(self) -> None:
        """Rescan the directory and update the file list."""
        self.files = scan_files(self.cwd)


class FileListControllerSnapshotter(StatefulOwningSnapshotter):
    """Snapshotter for FileListController - rescans on each snapshot."""

    def snapshot_owned(self, obj: FileListController) -> ThingState:
        return self.stateless_snapshot(obj)

    def stateless_snapshot(self, obj: FileListController) -> ThingState:
        # Rescan files on each snapshot to pick up changes
        obj.rescan()
        return ThingState(
            key=FILE_LIST_KEY,
            thing_type="FileList",
            thing_data={
                "cwd": obj.cwd,
                "files": obj.files,
                "count": len(obj.files),
            },
            links=[],
        )


class FileListControllerAnchorCapturer(AnchorCapturer):
    """Anchor capturer for FileListController - returns a ref to the file list thing."""

    def handle(self, anchor: FileListController) -> ThingRef:  # noqa: ARG002
        return ThingRef(key=FILE_LIST_KEY)
