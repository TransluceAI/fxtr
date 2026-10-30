"""Snapshotter and anchor capturer for ClydraManager."""

from __future__ import annotations

from clydra.file_list import FILE_LIST_KEY
from clydra.interrupt import INTERRUPT_KEY
from clydra.manager import ClydraManager
from clydra.model import MODEL_KEY
from clydra.permission_dialog import PERMISSION_DIALOG_KEY
from clydra.permission_mode import PERMISSION_MODE_KEY
from thingwatcher import ThingDelta, ThingLink, ThingRef, ThingState
from thingwatcher.api import AnchorCapturer, StatefulOwningSnapshotter

# The key for the root thing
CLYDRA_ROOT_KEY = ["clydra_root"]

# Links to child things
CHILD_LINKS = [
    ThingLink(
        link_type="interrupt",
        link_key="interrupt",
        to_thing_key=INTERRUPT_KEY,
    ),
    ThingLink(
        link_type="permission_mode",
        link_key="permission_mode",
        to_thing_key=PERMISSION_MODE_KEY,
    ),
    ThingLink(
        link_type="model",
        link_key="model",
        to_thing_key=MODEL_KEY,
    ),
    ThingLink(
        link_type="permission_dialog",
        link_key="permission_dialog",
        to_thing_key=PERMISSION_DIALOG_KEY,
    ),
    ThingLink(
        link_type="file_list",
        link_key="file_list",
        to_thing_key=FILE_LIST_KEY,
    ),
]


class ClydraManagerSnapshotter(StatefulOwningSnapshotter):
    """Snapshotter for ClydraManager.

    Uses ThingState for initial snapshot and ThingDelta with pop_chunks/push_chunk
    for incremental updates. Cooperates with manager's pending ops to compute
    minimal deltas for append, pop, and clear operations.

    IMPORTANT: Chunk tracking invariant
    -----------------------------------
    We must track a chunk size in `_chunk_event_counts` every time we emit:
    - A ThingState (which creates a single chunk in the database)
    - A ThingDelta with push_chunk (even if the chunk has 0 events)

    This is because thingwatcher stores ALL pushed chunks in the database, including
    empty ones (e.g., state-only updates). If our tracking doesn't match the actual
    chunks in the database, pop_chunks will pop the wrong number and leave stale
    data visible.
    """

    def __init__(self) -> None:
        # Track chunk event counts per manager instance
        self._chunk_event_counts: dict[int, list[int]] = {}
        # Track whether first snapshot has been done
        self._initialized: dict[int, bool] = {}
        # Track last state for state-only updates
        self._last_state: dict[int, str] = {}

    def snapshot_owned(self, obj: ClydraManager) -> ThingState | ThingDelta:
        manager = obj
        obj_id = id(manager)

        # First snapshot: emit full ThingState
        if not self._initialized.get(obj_id):
            return self.stateless_snapshot(manager)

        # Consume pending ops from manager
        ops = manager._consume_pending_ops()
        current_state = manager.state
        last_state = self._last_state.get(obj_id)

        # If no ops, check for state-only change
        if not ops:
            if current_state != last_state:
                self._last_state[obj_id] = current_state
                # Track the empty chunk we're pushing
                self._chunk_event_counts[obj_id].append(0)
                return ThingDelta(
                    key=CLYDRA_ROOT_KEY,
                    push_chunk={
                        "events": [],
                        "state": manager.state,
                        "session_id": manager.session_id,
                        "transcript_path": manager.transcript_path,
                    },
                )
            # No changes at all
            return ThingDelta(key=CLYDRA_ROOT_KEY)

        # Process ops to compute (chunks_to_pop, suffix_len)
        chunk_counts = self._chunk_event_counts[obj_id]
        chunks_to_pop = 0
        suffix_len = 0

        for op in ops:
            op_type = op["op"]

            if op_type == "push":
                suffix_len += op["count"]

            elif op_type == "pop":
                n = op["count"]
                # "Borrow" from chunks while suffix is insufficient
                while suffix_len < n and chunks_to_pop < len(chunk_counts):
                    chunks_to_pop += 1
                    suffix_len += chunk_counts[-chunks_to_pop]
                suffix_len -= n

            elif op_type == "clear":
                # Pop all existing chunks, reset suffix
                chunks_to_pop = len(chunk_counts)
                suffix_len = 0

        # Get events to push
        events = manager.get_events()
        buffer_events = events[-suffix_len:] if suffix_len > 0 else []

        self._last_state[obj_id] = current_state

        # Emit delta and update chunk tracking
        if chunks_to_pop > 0 or buffer_events:
            # Update chunk tracking
            if chunks_to_pop > 0:
                # Remove popped chunks from end
                self._chunk_event_counts[obj_id] = chunk_counts[:-chunks_to_pop]
            # Track the chunk we're pushing (even if empty)
            self._chunk_event_counts[obj_id].append(len(buffer_events))

            return ThingDelta(
                key=CLYDRA_ROOT_KEY,
                pop_chunks=chunks_to_pop,
                push_chunk={
                    "events": buffer_events,
                    "state": manager.state,
                    "session_id": manager.session_id,
                    "transcript_path": manager.transcript_path,
                },
            )
        else:
            # No changes (shouldn't happen if we had ops, but handle gracefully)
            return ThingDelta(key=CLYDRA_ROOT_KEY)

    def stateless_snapshot(self, obj: ClydraManager) -> ThingState:
        """Stateless snapshot for hard recapture - returns full state without tracking."""
        manager = obj
        obj_id = id(manager)

        # Clear any pending ops (they're captured in full state)
        manager._consume_pending_ops()

        # Initialize/reset chunk tracking
        events = manager.get_events()
        self._chunk_event_counts[obj_id] = [len(events)]
        self._initialized[obj_id] = True
        self._last_state[obj_id] = manager.state

        return ThingState(
            key=CLYDRA_ROOT_KEY,
            thing_type="ClydraRoot",
            thing_data={
                "session_id": manager.session_id,
                "transcript_path": manager.transcript_path,
                "events": events,
                "state": manager.state,
            },
            links=CHILD_LINKS,
        )


class ClydraManagerAnchorCapturer(AnchorCapturer):
    """Anchor capturer for ClydraManager - returns a ref to the root thing."""

    def handle(self, anchor: ClydraManager) -> ThingRef:
        return ThingRef(key=CLYDRA_ROOT_KEY)
