"""Permission mode controller for clydra - handles permission mode changes in a separate task."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from thingwatcher import ThingRef, ThingState
from thingwatcher.api import AnchorCapturer, StatefulOwningSnapshotter

if TYPE_CHECKING:
    from clydra.manager import ClydraManager

# The key for the permission mode thing
PERMISSION_MODE_KEY = ["clydra_permission_mode"]

# Available permission modes (excluding bypassPermissions for safety)
PermissionMode = Literal["default", "acceptEdits", "plan"]
PERMISSION_MODES: list[PermissionMode] = ["default", "acceptEdits", "plan"]


@dataclass
class PermissionModeController:
    """Controller for permission mode selection.

    This is a separate tracked thing that handles permission mode changes
    in its own task, independent of the main message loop.

    Attributes:
        current_mode: The current permission mode.
    """

    current_mode: PermissionMode = "default"
    _manager: ClydraManager | None = field(default=None, repr=False)


class PermissionModeControllerSnapshotter(StatefulOwningSnapshotter):
    """Simple snapshotter for PermissionModeController - always returns full state."""

    def snapshot_owned(self, controller: PermissionModeController) -> ThingState:
        return self.stateless_snapshot(controller)

    def stateless_snapshot(self, controller: PermissionModeController) -> ThingState:
        return ThingState(
            key=PERMISSION_MODE_KEY,
            thing_type="PermissionMode",
            thing_data={"current_mode": controller.current_mode},
            links=[],
        )


class PermissionModeControllerAnchorCapturer(AnchorCapturer):
    """Anchor capturer for PermissionModeController - returns a ref to the permission mode thing."""

    def handle(self, anchor: PermissionModeController) -> ThingRef:
        return ThingRef(key=PERMISSION_MODE_KEY)
