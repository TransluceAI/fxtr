"""Basic tests for thingwatcher."""

import pytest
import thingwatcher as tw


class TestImports:
    """Test that all public API is importable."""

    def test_import_module(self) -> None:
        """Can import thingwatcher."""
        assert tw is not None

    def test_instrumentation_api_available(self) -> None:
        """Instrumentation functions are available."""
        assert callable(tw.track)
        assert callable(tw.untrack)
        assert callable(tw.tick)
        assert callable(tw.pause)
        assert callable(tw.receive)

    def test_configuration_api_available(self) -> None:
        """Configuration functions are available."""
        assert callable(tw.register_snapshotter)
        assert callable(tw.register_anchor_capturer)
        assert callable(tw.configure_rendering)

    def test_types_available(self) -> None:
        """Data types are available."""
        assert tw.ThingState is not None
        assert tw.ThingDelta is not None
        assert tw.ThingLink is not None
        assert tw.ThingRef is not None

    def test_watcher_class_available(self) -> None:
        """Watcher class is available."""
        assert tw.Watcher is not None


class TestNoopBehavior:
    """Test that instrumentation is no-op when no watcher is active."""

    def test_track_noop(self) -> None:
        """track() is a no-op without active watcher."""
        obj = object()
        tw.track(obj)  # Should not raise

    def test_untrack_noop(self) -> None:
        """untrack() is a no-op without active watcher."""
        obj = object()
        tw.untrack(obj)  # Should not raise

    def test_tick_noop(self) -> None:
        """tick() is a no-op without active watcher."""
        tw.tick("info", "test message")  # Should not raise

    @pytest.mark.asyncio
    async def test_pause_noop(self) -> None:
        """pause() is a no-op without active watcher."""
        anchor = object()
        await tw.pause(anchor)  # Should not raise

    @pytest.mark.asyncio
    async def test_receive_raises_without_watcher(self) -> None:
        """receive() raises NoWatcherConnectedError without active watcher."""
        anchor = object()
        with pytest.raises(tw.NoWatcherConnectedError):
            await tw.receive(anchor)


class TestDataTypes:
    """Test that data types can be instantiated."""

    def test_thing_link(self) -> None:
        """ThingLink can be created."""
        link = tw.ThingLink(
            link_type="owns",
            link_key="link1",
            to_thing_key="obj2",
        )
        assert link.link_type == "owns"
        assert link.link_key == "link1"
        assert link.to_thing_key == "obj2"

    def test_thing_state(self) -> None:
        """ThingState can be created."""
        state = tw.ThingState(
            key="obj1",
            thing_type="MyClass",
            thing_data={"value": 42},
            links=[],
        )
        assert state.key == "obj1"
        assert state.thing_type == "MyClass"

    def test_thing_delta(self) -> None:
        """ThingDelta can be created."""
        delta = tw.ThingDelta(
            key="obj1",
            pop_chunks=tw.POP_ALL,
            push_chunk={"value": 43},
        )
        assert isinstance(delta.pop_chunks, type(tw.POP_ALL))

    def test_thing_ref(self) -> None:
        """ThingRef can be created."""
        ref = tw.ThingRef(key="obj1")
        assert ref.key == "obj1"


class TestWatcher:
    """Test Watcher class."""

    def test_watcher_creation(self) -> None:
        """Watcher can be created with default params."""
        watcher = tw.Watcher()
        assert watcher.port == 8765
        assert watcher.db_path is None

    def test_watcher_custom_params(self) -> None:
        """Watcher can be created with custom params."""
        watcher = tw.Watcher(db_path="/tmp/test.db", port=9000)
        assert watcher.db_path == "/tmp/test.db"
        assert watcher.port == 9000

    @pytest.mark.asyncio
    async def test_watch_and_serve_until(self) -> None:
        """watch_and_serve_until runs the provided coroutine."""
        watcher = tw.Watcher(port=None)
        result = await watcher.watch_and_serve_until(async_return_42)
        assert result == 42


async def async_return_42() -> int:
    """Helper coroutine that returns 42."""
    return 42
