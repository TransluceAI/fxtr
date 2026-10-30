"""Tests for pause/trigger mechanism."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import anyio
import pytest
from thingwatcher.api import (
    NoWatcherConnectedError,
    StatelessSnapshotter,
    ThingRef,
    ThingState,
    Watcher,
    pause,
    receive,
    register_anchor_capturer,
)
from thingwatcher.registry import clear_global_registries


@dataclass
class PauseAnchor:
    """Simple test anchor object for pause tests."""

    name: str
    value: int = 0


class TestAnchorCapturer:
    """AnchorCapturer for PauseAnchor."""

    def handle(self, anchor: PauseAnchor) -> ThingState:
        return ThingState(
            key=("test_anchor", anchor.name),
            thing_type="test_anchor",
            thing_data={"name": anchor.name, "value": anchor.value},
            links=[],
        )


class PauseAnchorRefCapturer:
    """AnchorCapturer that returns ThingRef for already-tracked objects."""

    def handle(self, anchor: PauseAnchor) -> ThingRef:
        return ThingRef(key=("test_anchor", anchor.name))


class PauseAnchorSnapshotter(StatelessSnapshotter):
    """Snapshotter for PauseAnchor (when also tracked)."""

    def snapshot(self, obj: PauseAnchor) -> ThingState:
        return ThingState(
            key=("test_anchor", obj.name),
            thing_type="test_anchor",
            thing_data={"name": obj.name, "value": obj.value},
            links=[],
        )


@pytest.fixture(autouse=True)
def cleanup_registries():
    """Clear global registries before and after each test."""
    clear_global_registries()
    yield
    clear_global_registries()


class TestBasicPause:
    """Tests for basic pause functionality."""

    async def test_pause_and_trigger(self):
        """Test that pause suspends and trigger resumes."""
        watcher = Watcher(port=None)
        watcher.register_anchor_capturer(PauseAnchor, TestAnchorCapturer())

        anchor = PauseAnchor(name="test1")
        result_holder: list[str] = []

        async def pausing_task():
            result_holder.append("before_pause")
            await watcher.pause(anchor)
            result_holder.append("after_pause")

        async def trigger_task():
            # Wait a bit for pause to be set up
            await anyio.sleep(0.05)
            # Get the key for the anchor
            triggerable = watcher.get_triggerable_things()
            assert len(triggerable) == 1
            key_bytes, thing_type = triggerable[0]
            # thing_type comes from the ThingState returned by the capturer
            assert thing_type == "test_anchor"

            # Trigger it
            result = await watcher.trigger_pause(key_bytes, None)
            assert result.ok
            assert result.trigger_id is not None

        async def main():
            async with anyio.create_task_group() as tg:
                tg.start_soon(pausing_task)
                tg.start_soon(trigger_task)

        await watcher.watch_and_serve_until(main)

        assert result_holder == ["before_pause", "after_pause"]

    async def test_receive_returns_payload(self):
        """Test that receive() returns the trigger payload."""
        watcher = Watcher(port=None)
        watcher.register_anchor_capturer(PauseAnchor, TestAnchorCapturer())

        anchor = PauseAnchor(name="test1")
        payload_holder: list[Any] = []

        async def receiving_task():
            payload = await watcher.receive(anchor)
            payload_holder.append(payload)

        async def trigger_task():
            await anyio.sleep(0.05)
            triggerable = watcher.get_triggerable_things()
            assert len(triggerable) == 1
            key_bytes, _ = triggerable[0]

            result = await watcher.trigger_pause(key_bytes, {"action": "continue", "value": 42})
            assert result.ok

        async def main():
            async with anyio.create_task_group() as tg:
                tg.start_soon(receiving_task)
                tg.start_soon(trigger_task)

        await watcher.watch_and_serve_until(main)

        assert len(payload_holder) == 1
        assert payload_holder[0] == {"action": "continue", "value": 42}

    async def test_pause_appears_in_tick(self):
        """Test that paused thing appears in tick snapshot with tick_after=True."""
        watcher = Watcher(port=None)
        watcher.register_anchor_capturer(PauseAnchor, TestAnchorCapturer())

        anchor = PauseAnchor(name="test1", value=123)
        tick_things: list[ThingState] = []

        async def pausing_task():
            # tick_after=True means it will tick immediately after setting up pause
            await watcher.pause(anchor, tick_after=True)

        async def capture_task():
            await anyio.sleep(0.05)
            # Trigger to let the pause complete
            triggerable = watcher.get_triggerable_things()
            assert len(triggerable) == 1
            key_bytes, _ = triggerable[0]
            await watcher.trigger_pause(key_bytes, None)

        # Set interceptor to capture the tick result
        def interceptor(result):
            tick_things.extend(result.states)

        watcher.set_tick_interceptor(interceptor)

        async def main():
            async with anyio.create_task_group() as tg:
                tg.start_soon(pausing_task)
                tg.start_soon(capture_task)

        await watcher.watch_and_serve_until(main)

        # Check that the pause thing was captured
        assert len(tick_things) >= 1
        pause_thing = [t for t in tick_things if t.thing_type == "test_anchor"]
        assert len(pause_thing) >= 1
        assert pause_thing[0].key == ("test_anchor", "test1")
        assert pause_thing[0].thing_data == {"name": "test1", "value": 123}


class TestTriggerErrors:
    """Tests for trigger error cases."""

    async def test_trigger_without_active_pause_fails(self):
        """Test that triggering without an active pause returns error."""
        watcher = Watcher(port=None)
        watcher.register_anchor_capturer(PauseAnchor, TestAnchorCapturer())

        async def main():
            # Try to trigger a key that has no pause
            key_bytes = b"nonexistent_key"
            result = await watcher.trigger_pause(key_bytes, None)
            assert not result.ok
            assert result.error == "No active pause for this key"

        await watcher.watch_and_serve_until(main)

    async def test_duplicate_pause_raises_error(self):
        """Test that pausing on the same anchor object twice raises error."""
        watcher = Watcher(port=None)
        watcher.register_anchor_capturer(PauseAnchor, TestAnchorCapturer())

        anchor = PauseAnchor(name="test1")
        error_holder: list[Exception] = []

        async def first_pause():
            await watcher.pause(anchor)

        async def second_pause():
            await anyio.sleep(0.02)
            try:
                await watcher.pause(anchor)
            except ValueError as e:
                error_holder.append(e)
            # Trigger to unblock first pause
            triggerable = watcher.get_triggerable_things()
            if triggerable:
                key_bytes, _ = triggerable[0]
                await watcher.trigger_pause(key_bytes, None)

        async def main():
            async with anyio.create_task_group() as tg:
                tg.start_soon(first_pause)
                tg.start_soon(second_pause)

        await watcher.watch_and_serve_until(main)

        assert len(error_holder) == 1
        assert "cannot pause on same anchor object twice" in str(error_holder[0])


class TestMissingCapturer:
    """Tests for missing pause capturer."""

    async def test_missing_capturer_raises_error(self):
        """Test that pause without registered capturer raises ValueError."""
        watcher = Watcher(port=None)
        # Don't register any capturer

        anchor = PauseAnchor(name="test1")

        async def main():
            with pytest.raises(ValueError, match="no anchor capturer registered"):
                await watcher.pause(anchor)

        await watcher.watch_and_serve_until(main)


class TestThingRefHandling:
    """Tests for ThingRef behavior in pause capturers."""

    async def test_thingref_marks_existing_thing_triggerable(self):
        """Test that ThingRef makes the referenced tracked thing triggerable."""
        watcher = Watcher(port=None)
        watcher.register_snapshotter(PauseAnchor, PauseAnchorSnapshotter())
        watcher.register_anchor_capturer(PauseAnchor, PauseAnchorRefCapturer())

        anchor = PauseAnchor(name="tracked_obj", value=42)
        watcher.track(anchor)

        result_holder: list[str] = []

        async def pausing_task():
            result_holder.append("before_pause")
            await watcher.pause(anchor)
            result_holder.append("after_pause")

        async def trigger_task():
            await anyio.sleep(0.05)
            # The tracked thing should be triggerable
            triggerable = watcher.get_triggerable_things()
            assert len(triggerable) == 1
            key_bytes, _ = triggerable[0]

            result = await watcher.trigger_pause(key_bytes, None)
            assert result.ok

        async def main():
            async with anyio.create_task_group() as tg:
                tg.start_soon(pausing_task)
                tg.start_soon(trigger_task)

        await watcher.watch_and_serve_until(main)

        assert result_holder == ["before_pause", "after_pause"]


class TestOrphanThingRef:
    """Tests for ThingRef pointing to non-existent things."""

    async def test_orphan_thingref_creates_broken_thing(self):
        """Test that ThingRef without matching state creates broken thing."""
        watcher = Watcher(port=None)
        # Register capturer that returns ThingRef but NO snapshotter
        watcher.register_anchor_capturer(PauseAnchor, PauseAnchorRefCapturer())

        anchor = PauseAnchor(name="orphan", value=1)
        tick_things: list[ThingState] = []

        def interceptor(result):
            tick_things.extend(result.states)

        watcher.set_tick_interceptor(interceptor)

        async def pausing_task():
            # This will create an orphan ThingRef since anchor is not tracked
            await watcher.pause(anchor, tick_after=True)

        async def trigger_task():
            await anyio.sleep(0.05)
            # Trigger to let the pause complete
            triggerable = watcher.get_triggerable_things()
            if triggerable:
                key_bytes, _ = triggerable[0]
                await watcher.trigger_pause(key_bytes, None)

        async def main():
            async with anyio.create_task_group() as tg:
                tg.start_soon(pausing_task)
                tg.start_soon(trigger_task)

        await watcher.watch_and_serve_until(main)

        # Should have a broken thing
        broken_things = [t for t in tick_things if t.thing_type == "__broken__"]
        assert len(broken_things) >= 1
        assert "ThingRef has no matching state" in str(broken_things[0].thing_data)


class TestCancellation:
    """Tests for pause cancellation cleanup."""

    async def test_cancellation_cleans_up_pause(self):
        """Test that cancelling a paused task cleans up the pause state."""
        watcher = Watcher(port=None)
        watcher.register_anchor_capturer(PauseAnchor, TestAnchorCapturer())

        anchor = PauseAnchor(name="cancelled")

        async def pausing_task():
            await watcher.pause(anchor)

        async def cancel_task(scope):
            await anyio.sleep(0.05)
            # Verify pause is set up
            triggerable = watcher.get_triggerable_things()
            assert len(triggerable) == 1

            # Cancel the pausing task
            scope.cancel()

            # Wait a bit for cleanup
            await anyio.sleep(0.02)

            # Verify pause is cleaned up
            triggerable_after = watcher.get_triggerable_things()
            assert len(triggerable_after) == 0

            # Triggering should now fail
            key_bytes = triggerable[0][0]
            result = await watcher.trigger_pause(key_bytes, None)
            assert not result.ok

        async def main():
            async with anyio.create_task_group() as tg:
                scope = tg.cancel_scope
                tg.start_soon(pausing_task)
                tg.start_soon(cancel_task, scope)

        await watcher.watch_and_serve_until(main)


class TestMultipleAnchors:
    """Tests for multiple anchors with same key."""

    async def test_multiple_anchors_same_key_all_triggered(self):
        """Test that triggering wakes all anchors waiting on the same key."""
        watcher = Watcher(port=None)

        # Use a capturer that always returns the same key
        class SameKeyCapturer:
            def handle(self, anchor: PauseAnchor) -> ThingState:
                return ThingState(
                    key="shared_key",
                    thing_type="test_anchor",
                    thing_data={"name": anchor.name},
                    links=[],
                )

        watcher.register_anchor_capturer(PauseAnchor, SameKeyCapturer())

        anchor1 = PauseAnchor(name="first")
        anchor2 = PauseAnchor(name="second")
        result_holder: list[str] = []

        async def pause1():
            result_holder.append("pause1_start")
            await watcher.pause(anchor1, tick_after=False)
            result_holder.append("pause1_done")

        async def pause2():
            await anyio.sleep(0.01)  # Slightly after pause1
            result_holder.append("pause2_start")
            await watcher.pause(anchor2, tick_after=True)
            result_holder.append("pause2_done")

        async def trigger():
            await anyio.sleep(0.05)
            triggerable = watcher.get_triggerable_things()
            assert len(triggerable) == 1  # Same key
            key_bytes, _ = triggerable[0]

            # Single trigger should wake both
            result = await watcher.trigger_pause(key_bytes, None)
            assert result.ok

        async def main():
            async with anyio.create_task_group() as tg:
                tg.start_soon(pause1)
                tg.start_soon(pause2)
                tg.start_soon(trigger)

        await watcher.watch_and_serve_until(main)

        # Both should have completed
        assert "pause1_done" in result_holder
        assert "pause2_done" in result_holder


class TestNoWatcher:
    """Tests for behavior without active watcher."""

    async def test_pause_without_watcher_is_noop(self):
        """Test that pause() without watcher returns immediately."""
        anchor = PauseAnchor(name="test")
        # Should not hang or raise
        await pause(anchor)

    async def test_receive_without_watcher_raises(self):
        """Test that receive() without watcher raises NoWatcherConnectedError."""
        anchor = PauseAnchor(name="test")
        with pytest.raises(NoWatcherConnectedError):
            await receive(anchor)


class TestKeyConflict:
    """Tests for key conflict handling during unification."""

    async def test_different_states_same_key_produces_broken_thing(self):
        """Test that conflicting states for same key produce broken thing."""
        watcher = Watcher(port=None)

        # Create two anchors that produce same key but different data
        class ConflictingCapturer1:
            def handle(self, anchor: PauseAnchor) -> ThingState:
                return ThingState(
                    key="conflict_key",
                    thing_type="test_anchor",
                    thing_data={"source": "capturer1", "value": 100},
                    links=[],
                )

        class ConflictingCapturer2:
            def handle(self, anchor: PauseAnchor) -> ThingState:
                return ThingState(
                    key="conflict_key",
                    thing_type="test_anchor",
                    thing_data={"source": "capturer2", "value": 200},  # Different!
                    links=[],
                )

        watcher.register_anchor_capturer("type1", ConflictingCapturer1())
        watcher.register_anchor_capturer("type2", ConflictingCapturer2())

        anchor1 = PauseAnchor(name="first")
        anchor2 = PauseAnchor(name="second")
        tick_things: list[ThingState] = []

        def interceptor(result):
            tick_things.extend(result.states)

        watcher.set_tick_interceptor(interceptor)

        async def pause1():
            await watcher.pause(anchor1, "type1", tick_after=False)

        async def pause2():
            await anyio.sleep(0.01)
            await watcher.pause(anchor2, "type2", tick_after=True)

        async def trigger():
            await anyio.sleep(0.05)
            triggerable = watcher.get_triggerable_things()
            if triggerable:
                for key_bytes, _ in triggerable:
                    await watcher.trigger_pause(key_bytes, None)

        async def main():
            async with anyio.create_task_group() as tg:
                tg.start_soon(pause1)
                tg.start_soon(pause2)
                tg.start_soon(trigger)

        await watcher.watch_and_serve_until(main)

        # Should have a broken thing due to conflict
        broken_things = [t for t in tick_things if t.thing_type == "__broken__"]
        assert len(broken_things) >= 1
        assert "different data" in str(broken_things[0].thing_data)


class TestGlobalAnchorCapturer:
    """Tests for global anchor capturer registration."""

    async def test_global_anchor_capturer_works(self):
        """Test that globally registered anchor capturer works."""
        # Register globally
        register_anchor_capturer(PauseAnchor, TestAnchorCapturer())

        watcher = Watcher(port=None)
        anchor = PauseAnchor(name="global_test")

        async def pausing_task():
            await watcher.pause(anchor)

        async def trigger_task():
            await anyio.sleep(0.05)
            triggerable = watcher.get_triggerable_things()
            assert len(triggerable) == 1
            key_bytes, _ = triggerable[0]
            await watcher.trigger_pause(key_bytes, None)

        async def main():
            async with anyio.create_task_group() as tg:
                tg.start_soon(pausing_task)
                tg.start_soon(trigger_task)

        await watcher.watch_and_serve_until(main)
