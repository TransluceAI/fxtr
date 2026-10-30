"""Integration tests for UVManagedPyImplProvider.

These tests mirror the functionality of test_in_process_py_provider.py but use the UV provider
to create isolated virtual environments and run implementations in subprocesses.
"""

import pathlib
import tempfile

import logfire
import pytest
from frozendict import frozendict
from fxtr_testdeps import impl_fixtures

import fxtr
from fxtr.impl_plans import py_impl_plan
from fxtr.object_model import core
from fxtr.providers import provider_base, uv_py_provider


@pytest.fixture
def anyio_backend():
    return "asyncio"


class MyTestDataEntity(
    core.DataEntityBase, namespace_id="!testing.test_uv_provider", type_id="MyTestDataEntity^1"
):
    """Test data entity for UV provider tests."""

    a: int
    b: float


class TestUVManagedPyImplProvider:
    """Integration test suite for UVManagedPyImplProvider."""

    @pytest.fixture(autouse=True)
    async def setup_method(self):
        """Set up test fixtures for each test method."""
        logfire.configure(
            send_to_logfire="if-token-present", inspect_arguments=False, distributed_tracing=True
        )

        # Create temporary directory for virtual environments
        self.temp_dir = tempfile.TemporaryDirectory(delete=True)
        # self.temp_dir = tempfile.TemporaryDirectory(delete=False)
        # print("NOT CLEANING UP")
        print(
            f"Created temporary directory: {self.temp_dir.name} ->"
            f" exists:{pathlib.Path(self.temp_dir.name).exists()}"
        )
        self.virtual_env_dir = pathlib.Path(self.temp_dir.name)

        # Create UV config with local fxtr package sources
        fxtr_path = pathlib.Path(fxtr.__file__).parent.parent.parent
        fxtr_testdeps_path = (
            pathlib.Path(fxtr.__file__).parent.parent.parent / "tests" / "fxtr_testdeps"
        )
        self.uv_config = {
            "sources": {
                "fxtr": {"path": str(fxtr_path), "editable": True},
                "fxtr_testdeps": {"path": str(fxtr_testdeps_path), "editable": True},
            }
        }

        token = uv_py_provider._FAIL_ON_KILL.set(True)
        # Store config for use in individual tests (provider will be created per test)
        try:
            yield
        finally:
            uv_py_provider._FAIL_ON_KILL.reset(token)

        # Cleanup
        self.temp_dir.cleanup()

    @pytest.mark.anyio
    async def test_provider_instantiation(self):
        """Test that the provider can be instantiated."""
        # Create a simple plan for testing
        impl_plan = py_impl_plan.PyImplPlan(
            interface_spec=core.spec_of(
                impl_fixtures.SequenceProcessor[core.AnyEntity, core.AdHocMetadata]
            ),
            requires="fxtr_testdeps>=0.0.1",
            constructor="fxtr_testdeps.impl_fixtures:SimpleWrapperImpl",
            kwargs=frozendict({"config": impl_fixtures.SimpleWrapperImplConfig(foo="test_value")}),
            impl_deps=frozenset(),
        )

        async with uv_py_provider.running_uv_managed_provider(
            virtual_env_dir=self.virtual_env_dir,
            plan_provenance_pairs=[(impl_plan, None)],
            uv_config=self.uv_config,
            remove_env_on_exit=True,
        ) as provider:
            assert isinstance(provider, uv_py_provider.UVManagedPyImplProvider)

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

        async with uv_py_provider.running_uv_managed_provider(
            virtual_env_dir=self.virtual_env_dir,
            plan_provenance_pairs=[(impl_plan, None)],
            uv_config=self.uv_config,
            remove_env_on_exit=True,
        ) as provider:
            # Provide the implementation using the new single-plan API
            route, provenance = await provider.provide(impl_plan, dependencies={})

            # Verify the route is a FxtrCallTarget (RemoteImpl since it's served in a subprocess)
            assert isinstance(route, core.FxtrCallTarget)
            assert route.interface_spec() == core.spec_of(
                impl_fixtures.SequenceProcessor[core.AnyEntity, core.AdHocMetadata]
            )

            # Verify provenance
            assert isinstance(provenance, uv_py_provider.UVManagedPyImplProvenance)
            assert "fxtr_testdeps" in [
                req.split(">=")[0].split("==")[0] for req in provenance.requirements
            ]
            assert provenance.uv_lock  # Should have lockfile content
            assert provenance.python_version  # Should have python version

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

        async with uv_py_provider.running_uv_managed_provider(
            virtual_env_dir=self.virtual_env_dir,
            plan_provenance_pairs=[(impl_plan, None)],
            uv_config=self.uv_config,
            remove_env_on_exit=True,
        ) as provider:
            route, _provenance = await provider.provide(impl_plan, dependencies={})

            # Verify we got a FxtrCallTarget that we can call directly
            assert isinstance(route, core.FxtrCallTarget)
            assert core.implements_interface(
                route, impl_fixtures.SequenceProcessor[core.AnyEntity, core.AdHocMetadata]
            )

            # Create test data
            test_data = [
                MyTestDataEntity(a=1, b=2.0),
                MyTestDataEntity(a=3, b=4.0),
            ]

            # Execute the implementation directly
            result = await route.process_seq(test_data)

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
    @pytest.mark.parametrize(
        "inner_requires,outer_requires",
        [
            pytest.param(
                "fxtr_testdeps>=0.0.1",
                "fxtr_testdeps>=0.0.1",
                id="compatible_requirements_shared_env",
            ),
            pytest.param(
                "fxtr_testdeps>=0.0.1\ntyping_extensions==4.14.0",
                "fxtr_testdeps>=0.0.1\ntyping_extensions==4.15.0",
                id="incompatible_requirements_separate_envs",
            ),
        ],
    )
    async def test_repeater_impl_provenance_locking(self, inner_requires: str, outer_requires: str):
        """Test repeater implementation with various provenance locking scenarios."""

        # Define plans once
        inner_plan = py_impl_plan.PyImplPlan(
            interface_spec=core.spec_of(
                impl_fixtures.SequenceProcessor[core.AnyEntity, core.AdHocMetadata]
            ),
            requires=inner_requires,
            constructor="fxtr_testdeps.impl_fixtures:SimpleWrapperImpl",
            kwargs=frozendict({"config": impl_fixtures.SimpleWrapperImplConfig(foo="inner_value")}),
            impl_deps=frozenset(),
        )
        outer_plan = py_impl_plan.PyImplPlan(
            interface_spec=core.spec_of(
                impl_fixtures.SequenceProcessor[core.AnyEntity, core.AdHocMetadata]
            ),
            requires=outer_requires,
            constructor="fxtr_testdeps.impl_fixtures:repeater_impl_entrypoint",
            kwargs=frozendict(
                {"config": impl_fixtures.RepeaterImplConfig(inner=inner_plan, repeats=2)}
            ),
            impl_deps=frozenset({inner_plan}),
        )

        async def run_and_execute(inner_provenance_lock=None, outer_provenance_lock=None):
            # Create provenance pairs for provider setup
            provenance_pairs = [
                (inner_plan, inner_provenance_lock),
                (outer_plan, outer_provenance_lock),
            ]

            async with uv_py_provider.running_uv_managed_provider(
                virtual_env_dir=self.virtual_env_dir,
                plan_provenance_pairs=provenance_pairs,
                uv_config=self.uv_config,
                remove_env_on_exit=True,
            ) as provider:
                # Provide the inner plan first
                inner_route, inner_provenance = await provider.provide(inner_plan, dependencies={})

                # Provide the outer plan with the inner plan as a dependency
                outer_route, outer_provenance = await provider.provide(
                    outer_plan, dependencies={inner_plan: inner_route}
                )
                assert core.implements_interface(
                    outer_route, impl_fixtures.SequenceProcessor[core.AnyEntity, core.AdHocMetadata]
                )

                # Execute the outer implementation directly
                assert isinstance(outer_route, core.FxtrCallTarget)

                test_data = [MyTestDataEntity(a=1, b=2.0), MyTestDataEntity(a=3, b=4.0)]
                result = await outer_route.process_seq(test_data)

                return result, inner_provenance, outer_provenance

        # Expected result
        expected = [
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

        # 1. Unlocked run to collect provenance
        result, inner_provenance, outer_provenance = await run_and_execute()
        assert result == expected

        # 2-4. Test locking scenarios
        locking_scenarios = [
            ("full", inner_provenance, outer_provenance),
            ("inner_only", inner_provenance, None),
            ("outer_only", None, outer_provenance),
        ]

        for lock_desc, inner_lock, outer_lock in locking_scenarios:
            try:
                result, _, _ = await run_and_execute(inner_lock, outer_lock)
                assert result == expected
            except Exception as e:
                e.add_note(f"Failed with {lock_desc} provenance lock")
                raise

    @pytest.mark.anyio
    async def test_invalid_plan_type_error(self):
        """Test that providing non-PyImplPlan objects raises an error."""
        # Create a simple valid plan for the provider setup
        valid_plan = py_impl_plan.PyImplPlan(
            interface_spec=core.spec_of(
                impl_fixtures.SequenceProcessor[core.AnyEntity, core.AdHocMetadata]
            ),
            requires="fxtr_testdeps>=0.0.1",
            constructor="fxtr_testdeps.impl_fixtures:SimpleWrapperImpl",
            kwargs=frozendict({"config": impl_fixtures.SimpleWrapperImplConfig(foo="test")}),
            impl_deps=frozenset(),
        )

        async with uv_py_provider.running_uv_managed_provider(
            plan_provenance_pairs=[(valid_plan, None)],
            virtual_env_dir=self.virtual_env_dir,
            uv_config=self.uv_config,
            remove_env_on_exit=True,
        ) as provider:
            # Create a mock plan that's not a PyImplPlan
            class MockPlan:
                pass

            mock_plan = MockPlan()

            # Should raise ProviderUnsupportedPlanError
            with pytest.raises(provider_base.ProviderUnsupportedPlanError):
                await provider.provide(mock_plan, dependencies={})  # type: ignore

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

        async with uv_py_provider.running_uv_managed_provider(
            plan_provenance_pairs=[(invalid_plan, None)],
            virtual_env_dir=self.virtual_env_dir,
            uv_config=self.uv_config,
            remove_env_on_exit=True,
        ) as provider:
            # Should raise an error (could be InvalidPlanError or RuntimeError from subprocess)
            with pytest.raises(Exception):  # Being permissive about the exact error type
                await provider.provide(invalid_plan, dependencies={})

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

        async with uv_py_provider.running_uv_managed_provider(
            virtual_env_dir=self.virtual_env_dir,
            plan_provenance_pairs=[(impl_plan, None)],
            uv_config=self.uv_config,
            remove_env_on_exit=True,
        ) as provider:
            _, provenance = await provider.provide(impl_plan, dependencies={})

            # Verify provenance structure
            assert isinstance(provenance, uv_py_provider.UVManagedPyImplProvenance)
            assert isinstance(provenance.requirements, tuple)
            assert isinstance(provenance.uv_lock, str)
            assert isinstance(provenance.python_version, str)

            # Should include the fxtr_testdeps package
            req_names = [req.split(">=")[0].split("==")[0] for req in provenance.requirements]
            assert "fxtr_testdeps" in req_names

            # UV lock should contain actual lockfile data
            assert "version = " in provenance.uv_lock  # UV lockfile format
            assert len(provenance.uv_lock) > 100  # Should be substantial content

    @pytest.mark.anyio
    async def test_lazy_preparation(self):
        """Test that plans can be provided without upfront preparation (lazy instantiation)."""
        # Create two plans with compatible requirements
        plan1 = py_impl_plan.PyImplPlan(
            interface_spec=core.spec_of(
                impl_fixtures.SequenceProcessor[core.AnyEntity, core.AdHocMetadata]
            ),
            requires="fxtr_testdeps>=0.0.1",
            constructor="fxtr_testdeps.impl_fixtures:SimpleWrapperImpl",
            kwargs=frozendict({"config": impl_fixtures.SimpleWrapperImplConfig(foo="plan1")}),
            impl_deps=frozenset(),
        )

        plan2 = py_impl_plan.PyImplPlan(
            interface_spec=core.spec_of(
                impl_fixtures.SequenceProcessor[core.AnyEntity, core.AdHocMetadata]
            ),
            requires="fxtr_testdeps>=0.0.1",
            constructor="fxtr_testdeps.impl_fixtures:SimpleWrapperImpl",
            kwargs=frozendict({"config": impl_fixtures.SimpleWrapperImplConfig(foo="plan2")}),
            impl_deps=frozenset(),
        )

        # Create provider WITHOUT any plan_provenance_pairs
        async with uv_py_provider.running_uv_managed_provider(
            virtual_env_dir=self.virtual_env_dir,
            uv_config=self.uv_config,
            remove_env_on_exit=True,
        ) as provider:
            # Provide plan1 - should lazily prepare it
            route1, provenance1 = await provider.provide(plan1, dependencies={})
            assert isinstance(route1, core.FxtrCallTarget)
            assert isinstance(provenance1, uv_py_provider.UVManagedPyImplProvenance)
            assert core.implements_interface(
                route1, impl_fixtures.SequenceProcessor[core.AnyEntity, core.AdHocMetadata]
            )

            # Provide plan2 - should lazily prepare it and potentially reuse plan1's group
            route2, provenance2 = await provider.provide(plan2, dependencies={})
            assert isinstance(route2, core.FxtrCallTarget)
            assert isinstance(provenance2, uv_py_provider.UVManagedPyImplProvenance)
            assert core.implements_interface(
                route2, impl_fixtures.SequenceProcessor[core.AnyEntity, core.AdHocMetadata]
            )

            # Verify both implementations work
            test_data = [MyTestDataEntity(a=1, b=2.0)]

            result1 = await route1.process_seq(test_data)
            assert isinstance(result1, list)
            assert result1[0] == core.AdHocMetadata(metadata=frozendict({"foo": "plan1"}))

            result2 = await route2.process_seq(test_data)
            assert isinstance(result2, list)
            assert result2[0] == core.AdHocMetadata(metadata=frozendict({"foo": "plan2"}))
