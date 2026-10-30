"""Tests for snapshotter and anchor capturer validation.

These tests verify that malformed outputs from snapshotters and anchor capturers
are caught early and converted to broken things, rather than crashing the DB writer.
"""

import pytest

from thingwatcher import ThingDelta, ThingLink, ThingRef, ThingState, Watcher
from thingwatcher.api import StatefulOwningSnapshotter, StatelessSnapshotter


class TestSnapshotterValidation:
    """Test that malformed snapshotter outputs become broken things."""

    @pytest.fixture
    def watcher(self) -> Watcher:
        return Watcher()

    def test_thingstate_with_thingref_in_links(self, watcher: Watcher) -> None:
        """ThingState.links must contain ThingLink, not ThingRef."""

        class BadLinksSnapshotter(StatelessSnapshotter):
            def snapshot(self, obj):
                return ThingState(
                    key="test",
                    thing_type="Test",
                    thing_data={},
                    links=[ThingRef(key="other")],  # Wrong type!
                )

        watcher.register_snapshotter("BadLinks", BadLinksSnapshotter())

        result_holder = []
        watcher.set_tick_interceptor(lambda s: result_holder.append(s))

        class BadLinks:
            pass

        obj = BadLinks()
        watcher.track(obj, "BadLinks")
        watcher.tick("info")

        assert len(result_holder) == 1
        assert len(result_holder[0].states) == 1
        thing = result_holder[0].states[0]
        assert thing.thing_type == "__broken__"
        assert "ThingLink" in thing.thing_data["error"]

    def test_thingstate_with_non_string_thing_type(self, watcher: Watcher) -> None:
        """ThingState.thing_type must be a string."""

        class BadTypeSnapshotter(StatelessSnapshotter):
            def snapshot(self, obj):
                return ThingState(
                    key="test",
                    thing_type=123,  # Wrong type!
                    thing_data={},
                    links=[],
                )

        watcher.register_snapshotter("BadType", BadTypeSnapshotter())

        result_holder = []
        watcher.set_tick_interceptor(lambda s: result_holder.append(s))

        class BadType:
            pass

        obj = BadType()
        watcher.track(obj, "BadType")
        watcher.tick("info")

        assert len(result_holder) == 1
        thing = result_holder[0].states[0]
        assert thing.thing_type == "__broken__"
        assert "thing_type must be a string" in thing.thing_data["error"]

    def test_thingstate_with_non_cborable_key(self, watcher: Watcher) -> None:
        """ThingState.key must be CBOR-serializable."""

        class NonSerializable:
            pass

        class BadKeySnapshotter(StatelessSnapshotter):
            def snapshot(self, obj):
                return ThingState(
                    key=NonSerializable(),  # Not CBOR-serializable!
                    thing_type="Test",
                    thing_data={},
                    links=[],
                )

        watcher.register_snapshotter("BadKey", BadKeySnapshotter())

        result_holder = []
        watcher.set_tick_interceptor(lambda s: result_holder.append(s))

        class BadKey:
            pass

        obj = BadKey()
        watcher.track(obj, "BadKey")
        watcher.tick("info")

        assert len(result_holder) == 1
        thing = result_holder[0].states[0]
        assert thing.thing_type == "__broken__"
        assert "CBOR-serializable" in thing.thing_data["error"]

    def test_thingstate_with_non_cborable_data(self, watcher: Watcher) -> None:
        """ThingState.thing_data must be CBOR-serializable."""

        class NonSerializable:
            pass

        class BadDataSnapshotter(StatelessSnapshotter):
            def snapshot(self, obj):
                return ThingState(
                    key="test",
                    thing_type="Test",
                    thing_data=NonSerializable(),  # Not CBOR-serializable!
                    links=[],
                )

        watcher.register_snapshotter("BadData", BadDataSnapshotter())

        result_holder = []
        watcher.set_tick_interceptor(lambda s: result_holder.append(s))

        class BadData:
            pass

        obj = BadData()
        watcher.track(obj, "BadData")
        watcher.tick("info")

        assert len(result_holder) == 1
        thing = result_holder[0].states[0]
        assert thing.thing_type == "__broken__"
        assert "CBOR-serializable" in thing.thing_data["error"]

    def test_thingstate_with_links_not_a_list(self, watcher: Watcher) -> None:
        """ThingState.links must be a list."""

        class BadLinksTypeSnapshotter(StatelessSnapshotter):
            def snapshot(self, obj):
                return ThingState(
                    key="test",
                    thing_type="Test",
                    thing_data={},
                    links="not a list",  # Wrong type!
                )

        watcher.register_snapshotter("BadLinksType", BadLinksTypeSnapshotter())

        result_holder = []
        watcher.set_tick_interceptor(lambda s: result_holder.append(s))

        class BadLinksType:
            pass

        obj = BadLinksType()
        watcher.track(obj, "BadLinksType")
        watcher.tick("info")

        assert len(result_holder) == 1
        thing = result_holder[0].states[0]
        assert thing.thing_type == "__broken__"
        assert "links must be a list" in thing.thing_data["error"]

    def test_thinglink_with_non_string_link_type(self, watcher: Watcher) -> None:
        """ThingLink.link_type must be a string."""

        class BadLinkTypeSnapshotter(StatelessSnapshotter):
            def snapshot(self, obj):
                return ThingState(
                    key="test",
                    thing_type="Test",
                    thing_data={},
                    links=[
                        ThingLink(
                            link_type=123,  # Wrong type!
                            link_key="key",
                            to_thing_key="to",
                        )
                    ],
                )

        watcher.register_snapshotter("BadLinkType", BadLinkTypeSnapshotter())

        result_holder = []
        watcher.set_tick_interceptor(lambda s: result_holder.append(s))

        class BadLinkType:
            pass

        obj = BadLinkType()
        watcher.track(obj, "BadLinkType")
        watcher.tick("info")

        assert len(result_holder) == 1
        thing = result_holder[0].states[0]
        assert thing.thing_type == "__broken__"
        assert "link_type must be a string" in thing.thing_data["error"]

    def test_thinglink_with_non_cborable_link_key(self, watcher: Watcher) -> None:
        """ThingLink.link_key must be CBOR-serializable."""

        class NonSerializable:
            pass

        class BadLinkKeySnapshotter(StatelessSnapshotter):
            def snapshot(self, obj):
                return ThingState(
                    key="test",
                    thing_type="Test",
                    thing_data={},
                    links=[
                        ThingLink(
                            link_type="child",
                            link_key=NonSerializable(),  # Not CBOR-serializable!
                            to_thing_key="to",
                        )
                    ],
                )

        watcher.register_snapshotter("BadLinkKey", BadLinkKeySnapshotter())

        result_holder = []
        watcher.set_tick_interceptor(lambda s: result_holder.append(s))

        class BadLinkKey:
            pass

        obj = BadLinkKey()
        watcher.track(obj, "BadLinkKey")
        watcher.tick("info")

        assert len(result_holder) == 1
        thing = result_holder[0].states[0]
        assert thing.thing_type == "__broken__"
        assert "CBOR-serializable" in thing.thing_data["error"]


class TestOwningSnapshotterValidation:
    """Test validation for StatefulOwningSnapshotter outputs."""

    @pytest.fixture
    def watcher(self) -> Watcher:
        return Watcher()

    def test_thingdelta_with_non_int_pop_chunks(self, watcher: Watcher) -> None:
        """ThingDelta.pop_chunks must be int or POP_ALL."""

        class BadPopSnapshotter(StatefulOwningSnapshotter):
            def __init__(self):
                self.first_call = True

            def snapshot_owned(self, obj):
                if self.first_call:
                    self.first_call = False
                    return ThingState(key="test", thing_type="Test", thing_data={}, links=[])
                return ThingDelta(
                    key="test",
                    pop_chunks="not an int",  # Wrong type!
                )

            def stateless_snapshot(self, obj):
                return ThingState(key="test", thing_type="Test", thing_data={}, links=[])

        watcher.register_snapshotter("BadPop", BadPopSnapshotter())

        result_holder = []
        watcher.set_tick_interceptor(lambda s: result_holder.append(s))

        class BadPop:
            pass

        obj = BadPop()
        watcher.track(obj, "BadPop")
        watcher.tick("info")  # First tick - ThingState
        watcher.tick("info")  # Second tick - bad ThingDelta

        assert len(result_holder) == 2
        thing = result_holder[1].states[0]
        assert thing.thing_type == "__broken__"
        assert "pop_chunks must be int or POP_ALL" in thing.thing_data["error"]

    def test_thingdelta_with_bad_added_links(self, watcher: Watcher) -> None:
        """ThingDelta.added_links must contain ThingLink objects."""

        class BadAddedLinksSnapshotter(StatefulOwningSnapshotter):
            def __init__(self):
                self.first_call = True

            def snapshot_owned(self, obj):
                if self.first_call:
                    self.first_call = False
                    return ThingState(key="test", thing_type="Test", thing_data={}, links=[])
                return ThingDelta(
                    key="test",
                    added_links=["not a link"],  # Wrong type!
                )

            def stateless_snapshot(self, obj):
                return ThingState(key="test", thing_type="Test", thing_data={}, links=[])

        watcher.register_snapshotter("BadAddedLinks", BadAddedLinksSnapshotter())

        result_holder = []
        watcher.set_tick_interceptor(lambda s: result_holder.append(s))

        class BadAddedLinks:
            pass

        obj = BadAddedLinks()
        watcher.track(obj, "BadAddedLinks")
        watcher.tick("info")  # First tick - ThingState
        watcher.tick("info")  # Second tick - bad ThingDelta

        assert len(result_holder) == 2
        thing = result_holder[1].states[0]
        assert thing.thing_type == "__broken__"
        assert "ThingLink" in thing.thing_data["error"]

    def test_thingdelta_with_bad_removed_link_keys(self, watcher: Watcher) -> None:
        """ThingDelta.removed_link_keys must be CBOR-serializable."""

        class NonSerializable:
            pass

        class BadRemovedKeysSnapshotter(StatefulOwningSnapshotter):
            def __init__(self):
                self.first_call = True

            def snapshot_owned(self, obj):
                if self.first_call:
                    self.first_call = False
                    return ThingState(key="test", thing_type="Test", thing_data={}, links=[])
                return ThingDelta(
                    key="test",
                    removed_link_keys=[NonSerializable()],  # Not CBOR-serializable!
                )

            def stateless_snapshot(self, obj):
                return ThingState(key="test", thing_type="Test", thing_data={}, links=[])

        watcher.register_snapshotter("BadRemovedKeys", BadRemovedKeysSnapshotter())

        result_holder = []
        watcher.set_tick_interceptor(lambda s: result_holder.append(s))

        class BadRemovedKeys:
            pass

        obj = BadRemovedKeys()
        watcher.track(obj, "BadRemovedKeys")
        watcher.tick("info")  # First tick - ThingState
        watcher.tick("info")  # Second tick - bad ThingDelta

        assert len(result_holder) == 2
        thing = result_holder[1].states[0]
        assert thing.thing_type == "__broken__"
        assert "CBOR-serializable" in thing.thing_data["error"]

    def test_snapshotter_returns_wrong_type(self, watcher: Watcher) -> None:
        """Snapshotter must return ThingState, ThingDelta, or sequence of them."""

        class WrongReturnSnapshotter(StatelessSnapshotter):
            def snapshot(self, obj):
                return "not a ThingState"  # Wrong return type!

        watcher.register_snapshotter("WrongReturn", WrongReturnSnapshotter())

        result_holder = []
        watcher.set_tick_interceptor(lambda s: result_holder.append(s))

        class WrongReturn:
            pass

        obj = WrongReturn()
        watcher.track(obj, "WrongReturn")
        watcher.tick("info")

        assert len(result_holder) == 1
        thing = result_holder[0].states[0]
        assert thing.thing_type == "__broken__"
        assert "ThingState" in thing.thing_data["error"] or "expected" in thing.thing_data["error"]


class TestAnchorCapturerValidation:
    """Test validation for AnchorCapturer outputs."""

    @pytest.fixture
    def watcher(self) -> Watcher:
        return Watcher()

    def test_anchor_capturer_returns_thingdelta(self, watcher: Watcher) -> None:
        """AnchorCapturer must return ThingState or ThingRef, not ThingDelta."""

        class BadAnchorCapturer:
            def handle(self, anchor):
                return ThingDelta(key="test")  # Wrong type!

        watcher.register_anchor_capturer("BadAnchor", BadAnchorCapturer())

        result_holder = []
        watcher.set_tick_interceptor(lambda s: result_holder.append(s))

        class BadAnchor:
            pass

        watcher.add_root("bad_root", BadAnchor(), typename="BadAnchor")
        watcher.tick("info")

        assert len(result_holder) == 1
        # The root capture should have produced a broken thing
        assert any(s.thing_type == "__broken__" for s in result_holder[0].states)
        broken = [s for s in result_holder[0].states if s.thing_type == "__broken__"][0]
        assert "ThingState or ThingRef" in broken.thing_data["error"]

    def test_anchor_capturer_returns_invalid_thingstate(self, watcher: Watcher) -> None:
        """AnchorCapturer ThingState output is also validated."""

        class BadStateAnchorCapturer:
            def handle(self, anchor):
                return ThingState(
                    key="test",
                    thing_type=123,  # Wrong type!
                    thing_data={},
                    links=[],
                )

        watcher.register_anchor_capturer("BadStateAnchor", BadStateAnchorCapturer())

        result_holder = []
        watcher.set_tick_interceptor(lambda s: result_holder.append(s))

        class BadStateAnchor:
            pass

        watcher.add_root("bad_root", BadStateAnchor(), typename="BadStateAnchor")
        watcher.tick("info")

        assert len(result_holder) == 1
        assert any(s.thing_type == "__broken__" for s in result_holder[0].states)
        broken = [s for s in result_holder[0].states if s.thing_type == "__broken__"][0]
        assert "thing_type must be a string" in broken.thing_data["error"]

    def test_anchor_capturer_returns_invalid_thingref(self, watcher: Watcher) -> None:
        """AnchorCapturer ThingRef output is also validated."""

        class NonSerializable:
            pass

        class BadRefAnchorCapturer:
            def handle(self, anchor):
                return ThingRef(key=NonSerializable())  # Not CBOR-serializable!

        watcher.register_anchor_capturer("BadRefAnchor", BadRefAnchorCapturer())

        result_holder = []
        watcher.set_tick_interceptor(lambda s: result_holder.append(s))

        class BadRefAnchor:
            pass

        watcher.add_root("bad_root", BadRefAnchor(), typename="BadRefAnchor")
        watcher.tick("info")

        assert len(result_holder) == 1
        assert any(s.thing_type == "__broken__" for s in result_holder[0].states)
        broken = [s for s in result_holder[0].states if s.thing_type == "__broken__"][0]
        assert "CBOR-serializable" in broken.thing_data["error"]


class TestStreamingValidation:
    """Test validation for streaming_update outputs."""

    @pytest.fixture
    def watcher(self) -> Watcher:
        return Watcher()

    def test_streaming_update_with_invalid_output(self, watcher: Watcher) -> None:
        """streaming_update validates snapshotter output."""
        call_count = 0

        class BadStreamingSnapshotter(StatefulOwningSnapshotter):
            def snapshot_owned(self, obj):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return ThingState(key="test", thing_type="Test", thing_data={}, links=[])
                # Return invalid on subsequent calls
                return ThingState(
                    key="test",
                    thing_type=123,  # Wrong type!
                    thing_data={},
                    links=[],
                )

            def stateless_snapshot(self, obj):
                return ThingState(key="test", thing_type="Test", thing_data={}, links=[])

        watcher.register_snapshotter("BadStreaming", BadStreamingSnapshotter())

        class BadStreaming:
            pass

        obj = BadStreaming()
        watcher.track(obj, "BadStreaming")

        # First call should work
        watcher.streaming_update(obj)

        # Second call should produce broken thing, not crash
        watcher.streaming_update(obj)
        # If we get here without exception, the validation worked
