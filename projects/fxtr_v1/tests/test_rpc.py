"""Tests for RPC system functionality.

This test module tests the RPC system by wrapping implementations in RemoteImpl
around an ImplServer, testing that client-server communication works correctly.
"""

import anyio
import logfire
import pytest
from frozendict import frozendict
from fxtr_testdeps import impl_fixtures

from fxtr.impl_plans import py_impl_plan
from fxtr.impls import impl_remote, implserver
from fxtr.object_model import core


@pytest.fixture
def anyio_backend():
    return "asyncio"


class MyTestDataEntity(
    core.DataEntityBase, namespace_id="!testing.test_provider", type_id="MyTestDataEntity^1"
):
    """Test data entity for RPC tests."""

    a: int
    b: float


class TestRPC:
    """Test suite for RPC system."""

    def setup_method(self):
        """Set up test fixtures."""
        logfire.configure(
            send_to_logfire="if-token-present", inspect_arguments=False, distributed_tracing=True
        )

    @pytest.mark.anyio
    async def test_simple_wrapper_impl_execution(self):
        """Test executing a simple wrapper implementation over RPC."""
        # Create the implementation plan
        impl_plan = py_impl_plan.PyImplPlan(
            interface_spec=core.spec_of(
                impl_fixtures.SequenceProcessor[core.AnyEntity, core.AdHocMetadata]
            ),
            requires="fxtr_testdeps>=0.0.1",
            constructor="fxtr_testdeps.impl_fixtures:SimpleWrapperImpl",
            kwargs=frozendict({"config": impl_fixtures.SimpleWrapperImplConfig(foo="test_value")}),
            impl_deps=frozenset(),
        )

        # Create the local implementation
        local_impl = impl_fixtures.SimpleWrapperImpl(
            None, config=impl_fixtures.SimpleWrapperImplConfig(foo="test_value")
        )
        assert isinstance(local_impl, core.FxtrCallTarget)

        # Serve the implementation over RPC
        async with implserver.ImplServer().serving() as server:
            with server.serve_impl(local_impl) as url:
                # Connect to it via RemoteImpl
                async with impl_remote.RemoteImpl.connect(
                    interface_spec=core.spec_of(
                        impl_fixtures.SequenceProcessor[core.AnyEntity, core.AdHocMetadata]
                    ),
                    base_url=url,
                ) as remote_impl:
                    # Create test data
                    test_data = [
                        MyTestDataEntity(a=1, b=2.0),
                        MyTestDataEntity(a=3, b=4.0),
                    ]

                    # Execute the implementation via RPC
                    result = await remote_impl.process_seq(test_data)

                    assert result == [
                        core.AdHocMetadata(metadata=frozendict({"foo": "test_value"})),
                        core.AdHocMetadata(
                            metadata=frozendict(
                                {
                                    "list_of_entities": (
                                        MyTestDataEntity(a=1, b=2.0),
                                        MyTestDataEntity(a=3, b=4.0),
                                    )
                                }
                            )
                        ),
                    ]

    @pytest.mark.anyio
    async def test_repeater_impl_with_dependencies(self):
        """Test executing an implementation with dependencies over RPC."""
        # Create the inner implementation
        inner_impl = impl_fixtures.SimpleWrapperImpl(
            None, config=impl_fixtures.SimpleWrapperImplConfig(foo="inner_value")
        )
        assert isinstance(inner_impl, core.FxtrCallTarget)

        # Serve the inner implementation over RPC
        async with implserver.ImplServer().serving() as inner_server:
            with inner_server.serve_impl(inner_impl) as inner_url:
                # Connect to the inner implementation via RemoteImpl
                async with impl_remote.RemoteImpl.connect(
                    interface_spec=core.spec_of(
                        impl_fixtures.SequenceProcessor[core.AnyEntity, core.AdHocMetadata]
                    ),
                    base_url=inner_url,
                ) as remote_inner_impl:
                    assert core.implements_interface(
                        remote_inner_impl, impl_fixtures.SequenceProcessor
                    )
                    # Create the repeater implementation that wraps the remote inner impl
                    repeater_impl = impl_fixtures.RepeaterImpl(
                        inner_processor=remote_inner_impl,
                        repeats=2,
                    )
                    assert isinstance(repeater_impl, core.FxtrCallTarget)

                    # Serve the repeater implementation over RPC
                    async with implserver.ImplServer().serving() as repeater_server:
                        with repeater_server.serve_impl(repeater_impl) as repeater_url:
                            # Connect to the repeater via RemoteImpl
                            async with impl_remote.RemoteImpl.connect(
                                interface_spec=core.spec_of(
                                    impl_fixtures.SequenceProcessor[
                                        core.AnyEntity, core.AdHocMetadata
                                    ]
                                ),
                                base_url=repeater_url,
                            ) as remote_repeater_impl:
                                # Create test data
                                test_data = [
                                    MyTestDataEntity(a=1, b=2.0),
                                    MyTestDataEntity(a=3, b=4.0),
                                ]

                                # Execute the repeater implementation via RPC
                                result = await remote_repeater_impl.process_seq(test_data)

                                assert result == [
                                    core.AdHocMetadata(metadata=frozendict({"foo": "inner_value"})),
                                    core.AdHocMetadata(
                                        metadata=frozendict(
                                            {
                                                "list_of_entities": (
                                                    MyTestDataEntity(a=1, b=2.0),
                                                    MyTestDataEntity(a=3, b=4.0),
                                                )
                                            }
                                        )
                                    ),
                                    core.AdHocMetadata(metadata=frozendict({"foo": "inner_value"})),
                                    core.AdHocMetadata(
                                        metadata=frozendict(
                                            {
                                                "list_of_entities": (
                                                    MyTestDataEntity(a=1, b=2.0),
                                                    MyTestDataEntity(a=3, b=4.0),
                                                )
                                            }
                                        )
                                    ),
                                ]

    @pytest.mark.anyio
    async def test_capability_passing_single_calculator(self):
        """Test passing a single calculator over RPC."""
        tracker_key = "test_single_calc"
        impl_fixtures.clear_finalization_tracker(tracker_key)

        # Create the factory implementation
        factory_impl = impl_fixtures.CalculatorFactoryImpl(tracker_key=tracker_key)

        # Serve the factory over RPC
        with anyio.fail_after(2):
            async with implserver.ImplServer().serving() as server:
                with server.serve_impl(factory_impl) as url:
                    # Connect to it via RemoteImpl
                    async with impl_remote.RemoteImpl.connect(
                        interface_spec=impl_fixtures.CalculatorFactory_spec,
                        base_url=url,
                    ) as remote_factory:
                        # Create a single calculator and use it
                        calc1 = await remote_factory.create_calculator("calc1")
                        assert await calc1.get_name() == "calc1"
                        assert await calc1.add(2, 3) == 5
                        assert await calc1.multiply(4, 5) == 20

                        # Verify no finalization yet
                        finalized_before = impl_fixtures.get_finalized_objects(tracker_key)
                        assert len(finalized_before) == 0, "No calculators should be finalized yet"

                        # Delete calc1 and verify it gets finalized
                        del calc1
                        await anyio.sleep(0.01)
                        finalized_after = impl_fixtures.get_finalized_objects(tracker_key)
                        assert (
                            "calc1" in finalized_after
                        ), "calc1 should be finalized after deletion"

        impl_fixtures.clear_finalization_tracker(tracker_key)

    @pytest.mark.anyio
    async def test_capability_passing_multiple_calculators(self):
        """Test passing multiple different calculators over RPC."""
        tracker_key = "test_multiple_calcs"
        impl_fixtures.clear_finalization_tracker(tracker_key)

        # Create the factory implementation
        factory_impl = impl_fixtures.CalculatorFactoryImpl(tracker_key=tracker_key)

        # Serve the factory over RPC
        with anyio.fail_after(2):
            async with implserver.ImplServer().serving() as server:
                with server.serve_impl(factory_impl) as url:
                    # Connect to it via RemoteImpl
                    async with impl_remote.RemoteImpl.connect(
                        interface_spec=impl_fixtures.CalculatorFactory_spec,
                        base_url=url,
                    ) as remote_factory:
                        # Create multiple calculators (different objects)
                        calcs = await remote_factory.create_multiple_calculators(3)
                        assert len(calcs) == 3

                        # Verify names and that they are different objects
                        assert await calcs[0].get_name() == "calc_1"
                        assert await calcs[1].get_name() == "calc_2"
                        assert await calcs[2].get_name() == "calc_3"
                        assert calcs[0] is not calcs[1]
                        assert calcs[1] is not calcs[2]
                        assert calcs[0] is not calcs[2]

                        assert await calcs[0].add(1, 2) == 3
                        assert await calcs[1].add(3, 4) == 7
                        assert await calcs[2].add(5, 6) == 11

                        # Verify no finalization yet
                        finalized_before = impl_fixtures.get_finalized_objects(tracker_key)
                        assert len(finalized_before) == 0, "No calculators should be finalized yet"

                        # Delete the list of calculators and verify they get finalized
                        del calcs
                        await anyio.sleep(0.01)
                        finalized_after = impl_fixtures.get_finalized_objects(tracker_key)
                        assert "calc_1" in finalized_after
                        assert "calc_2" in finalized_after
                        assert "calc_3" in finalized_after

        impl_fixtures.clear_finalization_tracker(tracker_key)

    @pytest.mark.anyio
    async def test_capability_passing_shared_calculator(self):
        """Test passing the same calculator multiple times over RPC."""
        tracker_key = "test_shared_calc"
        impl_fixtures.clear_finalization_tracker(tracker_key)

        # Create the factory implementation
        factory_impl = impl_fixtures.CalculatorFactoryImpl(tracker_key=tracker_key)

        # Serve the factory over RPC
        with anyio.fail_after(2):
            async with implserver.ImplServer().serving() as server:
                with server.serve_impl(factory_impl) as url:
                    # Connect to it via RemoteImpl
                    async with impl_remote.RemoteImpl.connect(
                        interface_spec=impl_fixtures.CalculatorFactory_spec,
                        base_url=url,
                    ) as remote_factory:
                        # Get shared calculator (same object returned multiple times)
                        shared_list = await remote_factory.get_shared_calculator_list(3)
                        shared1 = await remote_factory.get_shared_calculator()
                        shared2 = await remote_factory.get_shared_calculator()
                        assert len(shared_list) == 3

                        # Clear factory's reference so only client references keep it alive
                        await remote_factory.clear_shared_calculator()

                        # Verify both references have the same name
                        assert await shared1.get_name() == "shared"
                        assert await shared2.get_name() == "shared"

                        # Verify they are the SAME Python object (same identity)
                        assert (
                            shared1 is shared2
                        ), "shared1 and shared2 should be the same Python object"

                        assert await shared1.add(10, 20) == 30
                        assert await shared2.multiply(3, 7) == 21

                        # Verify all items are the same Python object as the previous references
                        assert shared_list[0] is shared1
                        assert shared_list[1] is shared1
                        assert shared_list[2] is shared1

                        # Verify they all have the same name
                        assert await shared_list[0].get_name() == "shared"
                        assert await shared_list[1].get_name() == "shared"
                        assert await shared_list[2].get_name() == "shared"

                        # Verify no finalization yet
                        finalized_before = impl_fixtures.get_finalized_objects(tracker_key)
                        assert len(finalized_before) == 0, "No calculators should be finalized yet"

                        # Delete one reference - should NOT finalize yet (4 references remaining)
                        del shared1
                        await anyio.sleep(0.01)
                        finalized_after_one = impl_fixtures.get_finalized_objects(tracker_key)
                        assert (
                            "shared" not in finalized_after_one
                        ), "shared calculator should not be finalized while references remain"

                        # Delete second reference - should NOT finalize yet (3 references remaining in list)
                        del shared2
                        await anyio.sleep(0.01)
                        finalized_after_two = impl_fixtures.get_finalized_objects(tracker_key)
                        assert (
                            "shared" not in finalized_after_two
                        ), "shared calculator should not be finalized while list references remain"

                        # Delete the list - should finalize now
                        del shared_list
                        await anyio.sleep(0.01)
                        finalized_final = impl_fixtures.get_finalized_objects(tracker_key)
                        assert (
                            "shared" in finalized_final
                        ), "shared calculator should be finalized after all references deleted"

        impl_fixtures.clear_finalization_tracker(tracker_key)
