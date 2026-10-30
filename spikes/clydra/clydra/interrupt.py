"""Interrupt controller for clydra - handles interrupt triggers in a separate task."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from thingwatcher import ThingRef, ThingState
from thingwatcher.api import AnchorCapturer, StatefulOwningSnapshotter

if TYPE_CHECKING:
    from clydra.manager import ClydraManager

# The key for the interrupt thing
INTERRUPT_KEY = ["clydra_interrupt"]


@dataclass
class InterruptController:
    """Controller for interrupt functionality.

    This is a separate tracked thing that handles interrupt triggers
    in its own task, independent of the main message loop.

    Attributes:
        enabled: True when streaming is active and interrupts are allowed.
    """

    enabled: bool = False
    _manager: ClydraManager | None = field(default=None, repr=False)


class InterruptControllerSnapshotter(StatefulOwningSnapshotter):
    """Simple snapshotter for InterruptController - always returns full state."""

    def snapshot_owned(self, controller: InterruptController) -> ThingState:
        return self.stateless_snapshot(controller)

    def stateless_snapshot(self, controller: InterruptController) -> ThingState:
        return ThingState(
            key=INTERRUPT_KEY,
            thing_type="InterruptButton",
            thing_data={"enabled": controller.enabled},
            links=[],
        )


class InterruptControllerAnchorCapturer(AnchorCapturer):
    """Anchor capturer for InterruptController - returns a ref to the interrupt thing."""

    def handle(self, anchor: InterruptController) -> ThingRef:
        return ThingRef(key=INTERRUPT_KEY)
