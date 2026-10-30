"""Model controller for clydra - handles model changes in a separate task."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from thingwatcher import ThingRef, ThingState
from thingwatcher.api import AnchorCapturer, StatefulOwningSnapshotter

if TYPE_CHECKING:
    from clydra.manager import ClydraManager

# The key for the model thing
MODEL_KEY = ["clydra_model"]


@dataclass
class ModelController:
    """Controller for model selection.

    This is a separate tracked thing that handles model changes
    in its own task, independent of the main message loop.

    Attributes:
        current_model: The current model name (empty string = unknown/default).
        error: True if the last set_model call failed.
    """

    current_model: str = ""
    error: bool = False
    _manager: ClydraManager | None = field(default=None, repr=False)


class ModelControllerSnapshotter(StatefulOwningSnapshotter):
    """Simple snapshotter for ModelController - always returns full state."""

    def snapshot_owned(self, obj: ModelController) -> ThingState:
        return self.stateless_snapshot(obj)

    def stateless_snapshot(self, obj: ModelController) -> ThingState:
        return ThingState(
            key=MODEL_KEY,
            thing_type="ModelPicker",
            thing_data={"current_model": obj.current_model, "error": obj.error},
            links=[],
        )


class ModelControllerAnchorCapturer(AnchorCapturer):
    """Anchor capturer for ModelController - returns a ref to the model thing."""

    def handle(self, anchor: ModelController) -> ThingRef:
        return ThingRef(key=MODEL_KEY)
