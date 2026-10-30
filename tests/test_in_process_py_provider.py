"""Tests for InProcessPyProvider functionality.

This test module covers the functionality demonstrated in nb_09_11.py, testing the
InProcessPyProvider's ability to instantiate and manage Python implementations.
"""

import pytest
from frozendict import frozendict
from fxtr_testdeps import impl_fixtures

from fxtr.impl_plans import py_impl_plan
from fxtr.object_model import core
from fxtr.providers import in_process_py_provider


@pytest.fixture
def anyio_backend():
    return "asyncio"


class MyTestDataEntity(
    core.DataEntityBase, namespace_id="!testing.test_provider", type_id="MyTestDataEntity^1"
):
    """Test data entity for provider tests."""

    a: int
    b: float


class TestInProcessPyProvider:
    """Test suite for InProcessPyProvider."""

    def setup_method(self):
        """Set up test fixtures."""
        self.provider = in_process_py_provider.InProcessPyProvider()

    def test_provider_instantiation(self):
        """Test that the provider can be instantiated."""
        assert isinstance(self.provider, in_process_py_provider.InProcessPyProvider)

    @pytest.mark.anyio
    async def test_simple_wrapper_impl_provision(self):
        """Test providing a simple wrapper implementation."""
        # Create a simple implementation plan
        impl_plan = py_impl_plan.PyImplPlan(
            interface_spec=core.spec_of(
                impl_fixtures.SequenceProcessor[core.AnyEntity, core.AdHocMetadata]
            ),
            requires="fxtr_testdeps>=0.0.1",
            constructor="fxtr_testdeps.impl_fixtures:SimpleWrapperImpl",
            kwargs=frozendict({"config": impl_fixtures.SimpleWrapperImplConfig(foo="test_value")}),
            impl_deps=frozenset(),
        )

        # Provide the implementation
        route, provenance = await self.provider.provide(impl_plan, dependencies={})

        # Verify we got back the expected structure

        # Verify the route is a FxtrCallTarget with the correct protocol
        assert route.interface_spec() == core.spec_of(
            impl_fixtures.SequenceProcessor[core.AnyEntity, core.AdHocMetadata]
        )

        # Verify provenance
        assert isinstance(provenance, in_process_py_provider.InProcessPyImplProvenance)
        assert "fxtr_testdeps" in provenance.package_versions

    @pytest.mark.anyio
    async def test_simple_wrapper_impl_execution(self):
        """Test executing a simple wrapper implementation."""
        # Create and provide implementation
        impl_plan = py_impl_plan.PyImplPlan(
            interface_spec=core.spec_of(
                impl_fixtures.SequenceProcessor[core.AnyEntity, core.AdHocMetadata]
            ),
            requires="fxtr_testdeps>=0.0.1",
            constructor="fxtr_testdeps.impl_fixtures:SimpleWrapperImpl",
            kwargs=frozendict({"config": impl_fixtures.SimpleWrapperImplConfig(foo="test_value")}),
            impl_deps=frozenset(),
        )

        route, provenance = await self.provider.provide(impl_plan, dependencies={})
        impl = route
        assert isinstance(impl, core.FxtrCallTarget)
        assert core.implements_interface(
            impl, impl_fixtures.SequenceProcessor[core.AnyEntity, core.AdHocMetadata]
        )

        # Create test data
        test_data = [
            MyTestDataEntity(a=1, b=2.0),
            MyTestDataEntity(a=3, b=4.0),
        ]

        # Execute the implementation
        result = await impl.process_seq(test_data)

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
        """Test providing and executing an implementation with dependencies."""
        # Create the inner implementation plan
        inner_plan = py_impl_plan.PyImplPlan(
            interface_spec=core.spec_of(
                impl_fixtures.SequenceProcessor[core.AnyEntity, core.AdHocMetadata]
            ),
            requires="fxtr_testdeps>=0.0.1",
            constructor="fxtr_testdeps.impl_fixtures:SimpleWrapperImpl",
            kwargs=frozendict({"config": impl_fixtures.SimpleWrapperImplConfig(foo="inner_value")}),
            impl_deps=frozenset(),
        )

        # Create the repeater implementation plan that depends on the inner one
        repeater_plan = py_impl_plan.PyImplPlan(
            interface_spec=core.spec_of(
                impl_fixtures.SequenceProcessor[core.AnyEntity, core.AdHocMetadata]
            ),
            requires="fxtr_testdeps>=0.0.1",
            constructor="fxtr_testdeps.impl_fixtures:repeater_impl_entrypoint",
            kwargs=frozendict(
                {"config": impl_fixtures.RepeaterImplConfig(inner=inner_plan, repeats=2)}
            ),
            impl_deps=frozenset({inner_plan}),
        )

        # Provide the inner implementation first
        inner_route, inner_provenance = await self.provider.provide(inner_plan, dependencies={})

        # Provide the repeater implementation with the inner implementation as dependency
        repeater_route, repeater_provenance = await self.provider.provide(
            repeater_plan, dependencies={inner_plan: inner_route}
        )

        # Get the repeater impl
        repeater_impl = repeater_route
        assert isinstance(repeater_impl, core.FxtrCallTarget)
        assert core.implements_interface(
            repeater_impl, impl_fixtures.SequenceProcessor[core.AnyEntity, core.AdHocMetadata]
        )

        # Create test data
        test_data = [
            MyTestDataEntity(a=1, b=2.0),
            MyTestDataEntity(a=3, b=4.0),
        ]

        # Execute the repeater implementation
        result = await repeater_impl.process_seq(test_data)

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
    async def test_invalid_plan_type_error(self):
        """Test that providing non-PyImplPlan objects raises an error."""

        # Create a mock plan that's not a PyImplPlan
        class MockPlan:
            pass

        mock_plan = MockPlan()

        # Should raise ProviderUnsupportedPlanError
        with pytest.raises(in_process_py_provider.provider_base.ProviderUnsupportedPlanError):
            await self.provider.provide(mock_plan, dependencies={})  # type: ignore

    @pytest.mark.anyio
    async def test_missing_entrypoint_error(self):
        """Test error handling for missing entrypoints."""
        # Create a plan with a non-existent entrypoint
        invalid_plan = py_impl_plan.PyImplPlan(
            interface_spec=core.spec_of(
                impl_fixtures.SequenceProcessor[core.AnyEntity, core.AdHocMetadata]
            ),
            requires="fxtr_testdeps>=0.0.1",
            constructor="fxtr_testdeps.impl_fixtures:nonexistent_entrypoint",
            kwargs=frozendict({}),
            impl_deps=frozenset(),
        )

        # Should raise InvalidPlanError
        with pytest.raises(in_process_py_provider.provider_base.InvalidPlanError):
            await self.provider.provide(invalid_plan, dependencies={})

    @pytest.mark.anyio
    async def test_provenance_tracking(self):
        """Test that provenance is correctly tracked."""
        impl_plan = py_impl_plan.PyImplPlan(
            interface_spec=core.spec_of(
                impl_fixtures.SequenceProcessor[core.AnyEntity, core.AdHocMetadata]
            ),
            requires="fxtr_testdeps>=0.0.1",
            constructor="fxtr_testdeps.impl_fixtures:SimpleWrapperImpl",
            kwargs=frozendict({"config": impl_fixtures.SimpleWrapperImplConfig(foo="test")}),
            impl_deps=frozenset(),
        )

        route, provenance = await self.provider.provide(impl_plan, dependencies={})

        # Verify provenance structure
        assert isinstance(provenance, in_process_py_provider.InProcessPyImplProvenance)
        assert isinstance(provenance.package_versions, frozendict)

        # Should include at least the fxtr_testdeps package
        assert "fxtr_testdeps" in provenance.package_versions

        # Package versions should be strings
        for package, version in provenance.package_versions.items():
            assert isinstance(package, str)
            assert isinstance(version, str)
