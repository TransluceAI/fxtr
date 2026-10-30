"""Provider that builds Python implementations using the UV package manager.

This is the refactored version using the new single-plan API and spawner-based architecture.

The UV provider works in the following stages:

### Stage 1: Partitioning.

The top-level builder takes a collection of PyImplPlans with optional provenance, along with some
configuration for how to spawn jobs. It then partitions the plans into groups such that each group
can be instantiated in a single environment.

First, we take each of the plans with locked provenance, and group them by the ones that have
_identical_ provenance records.

Next, we iteratively group the remaining plans. In each round, while there are still plans in the input plans list:

- Take the first plan from the input plans list. Remove the plan from the input plans list.
- Start a new group.
- Initialize a directory for the environment in the provider's virtual environment directory.
    Create a pyproject.toml file that lists the `behaviors` package (with current version) along
    with the dependencies of this plan, combined with the project's "tool.uv" config table(s).
- Run `uv lock` to resolve the dependencies.
- Iterate through plans that have not been handled yet.
    - If this plan's requirements are a _strict subset_ of the requirements already in the group,
      add it to the group and remove it from the input plans list.
    - If not, and if we have NOT yet failed to add a plan to the group, then:
        - Try adding its requirements to the pyproject.toml file, and running `uv lock` to install
            the dependencies.
        - If the call succeeds, add the plan to the group, mark it as handled by the group, and
            remove it from the input plans list.
        - If the call fails, unwind the changes, and set the flag so that we don't try to add new
          requirements to this group.
- Create a new provenance record for the entries in this group by reading the `uv.lock` file.
- Run `uv sync --locked` to install the dependencies.
- Finish this group.

### Stage 2a: Instantiating.

The builder now loops through each group. For each group:

- Create a random token.
- Start a temporary new local impl that waits to receive a spawner URL, and register it with
  the server singleton. The impl should have a route that receives the token and a spawner URL,
  checks the token, and then records the spawner URL.
- Use `uv run python -m behaviors.staging.providers.uv_py_provider_entrypoint [local_impl_url] [token]`
  to run the entrypoint script.
- Wait to receive a spawner URL from the subprocess over the local impl.
- (at this point we can exit the temporary local impl serving scope and stop serving the local impl)

Subprocesses should be run using anyio.open_process to make sure they last until the shutdown
signal.

### Stage 2b: Serving.

When behaviors.staging.providers.uv_py_provider_entrypoint runs, it will receive a URL it can use
to register itself on the server. It now needs to:
- Create a new ImplServer for itself, and a InProcessPyProvider.
- Create a spawner impl that instantiates implementations given the plans and the dep routes for
  them. The spawner can use the InProcessPyProvider to instantiate the implementation without
  provenance. It should then convert each implementation to an ASGI app, and add them into the
  server, returning the URLs for each implementation.
- Register this spawner impl on the server, and send the server's URL back to the parent process.
- Wait for a signal, and then exit cleanly.

### Stage 3: Providing.

Once all groups have been instantiated, the builder has enough implementation to instantiate the
actual provider, which routes plans to their correct spawner process.

The provider, when called, should make sure its provenance matches. It should then attach to the
spawner for the given plan, and send the plan and dep routes to it. The spawner should then return
the URL for the implementation. The provider can then create the appropriate route.
"""

from __future__ import annotations

import abc
import contextlib
import contextvars
import dataclasses
import logging
import os
import pathlib
import signal
import sys
import typing
import uuid
from typing import Any, AsyncGenerator, Awaitable, Callable, Mapping, Sequence, TypedDict

import anyio
import anyio.abc
import anyio.streams.memory
import tomlkit

from ..impl_plans.py_impl_plan import PyImplPlan
from ..impls import impl_remote
from ..impls.implserver import ImplServer
from ..object_model import core
from ..providers import provider_base

logger = logging.getLogger(__name__)


class DependencyPlanItem(TypedDict):
    """Represents a dependency plan and its implementation."""

    plan: core.Plan[Any]
    impl: core.FxtrCallTarget


# Internal protocol for spawner communication
class _UV_SPAWNER_PROTOCOL(
    core.FxtrCallTarget,
    interface_id="UVSpawnerProtocol^1",
    namespace_id="!builtin.uv_provider",
    stateless=False,
):
    """Protocol for spawning implementation processes."""

    @abc.abstractmethod
    async def spawn(
        self,
        plan: PyImplPlan,
        dependency_plan_items: list[DependencyPlanItem],
    ) -> core.FxtrCallTarget:
        """Spawn a new implementation for the given plan and dependencies.

        Returns:
            The spawned implementation
        """
        ...


# Internal protocol for registration communication
class _UV_REGISTER_PROTOCOL(
    core.FxtrCallTarget,
    interface_id="UVRegisterProtocol^1",
    namespace_id="!builtin.uv_provider",
    stateless=False,
):
    """Protocol for registering spawner URLs."""

    @abc.abstractmethod
    async def register(self, token: str, spawner_url: str) -> None:
        """Register a spawner URL with token validation."""
        ...


class UVManagedPyImplProvenance(
    core.DataEntityBase, namespace_id="!builtin.provenance", type_id="UVManagedPyImplProvenance^1"
):
    """Provenance for a Python implementation managed by UV.

    This provenance type is used when a Python implementation is run in an environment managed by
    UV, and does not have other dependencies outside of what UV can provide.

    Attributes:
        python_version: Python version that was used to instantiate the implementation.
        requirements: Requirements list that was used to instantiate the implementation.
        uv_lock: Contents of the UV lockfile that was used to instantiate the implementation.
    """

    python_version: str
    requirements: tuple[str, ...]
    uv_lock: str


@dataclasses.dataclass
class _PlanGroup:
    """Internal data structure representing a group of plans that can share an environment.

    Attributes:
        plans: List of protocol implementation plans in this group.
        virtual_env_path: Path to the virtual environment directory.
        provenance: Optional provenance information if using locked dependencies.
        python_version: Python version constraint used for this group.
        requirements: List of package requirements for this group.
        spawner: RemoteImpl of the spawner for this group (set after instantiation).
    """

    plans: list[PyImplPlan]
    virtual_env_path: pathlib.Path
    provenance: UVManagedPyImplProvenance | None
    python_version: str
    requirements: list[str] = dataclasses.field(default_factory=list)
    spawner: impl_remote.RemoteImpl | None = None


async def _race_events(**kwargs: Callable[[], Awaitable[Any]]) -> tuple[str, Any]:
    """Race multiple events, returning the name of the event that finishes first."""
    _result: str | None = None
    _result_value: Any | None = None
    async with anyio.create_task_group() as tg:

        async def _go(name: str, event: Callable[[], Awaitable[Any]]) -> None:
            nonlocal _result, _result_value
            _result_value = await event()
            assert _result is None
            _result = name
            tg.cancel_scope.cancel()

        for name, event in kwargs.items():
            tg.start_soon(_go, name, event)

    _result = typing.cast(str | None, _result)
    assert isinstance(_result, str)
    return _result, _result_value


_DEFAULT_PY_VERSION = f"{sys.version_info.major}.{sys.version_info.minor}.*"

_FAIL_ON_KILL = contextvars.ContextVar("_FAIL_ON_KILL", default=False)


class UVManagedPyImplProvider(provider_base.ImplProvider):
    """Provider that builds Python implementations using the UV package manager.

    This provider can dynamically instantiate implementations on demand using the single-plan API.
    Plans can be prepared ahead of time using prepare(), or lazily prepared during provide().

    Args:
        virtual_env_dir: Directory where virtual environments will be created.
        impl_server: Server for hosting implementations.
        task_group: Task group for managing background processes.
        shutdown_event: Event that signals when to shut down spawner processes.
        uv_config: Optional UV configuration to include in pyproject.toml.
        remove_env_on_exit: Whether to remove environments on cleanup.
        python_version: Python version constraint used for all environments.
    """

    def __init__(
        self,
        virtual_env_dir: pathlib.Path,
        impl_server: ImplServer,
        task_group: anyio.abc.TaskGroup,
        shutdown_event: anyio.Event,
        uv_config: dict | None = None,
        remove_env_on_exit: bool = True,
        python_version: str = _DEFAULT_PY_VERSION,
    ):
        self._virtual_env_dir = virtual_env_dir
        self._impl_server = impl_server
        self._task_group = task_group
        self._shutdown_event = shutdown_event
        self._uv_config = uv_config or {}
        self._remove_env_on_exit = remove_env_on_exit
        self._default_python_version = python_version

        # State tracking
        self._groups: list[_PlanGroup] = []
        self._plan_to_group: dict[PyImplPlan, _PlanGroup] = {}
        self._prepare_lock = anyio.Lock()

    async def prepare(
        self,
        plan_provenance_pairs: Sequence[tuple[core.Plan[Any], core.AnyEntity | None]],
    ) -> None:
        """Prepare plans by partitioning them into groups and instantiating spawners.

        This method can be called multiple times to incrementally add plans. It will:
        1. Try to reuse existing groups with exact provenance matches
        2. Try to assign plans to existing groups where requirements are a subset
        3. Partition and instantiate remaining plans into new groups

        Args:
            plan_provenance_pairs: Sequence of (plan, provenance) pairs to prepare.
        """
        async with self._prepare_lock:
            # Step 1: Try greedy assignment to existing groups
            remaining_plans = []
            for plan, provenance in plan_provenance_pairs:
                assert isinstance(plan, PyImplPlan), "Plan must be a PyImplPlan"
                assert provenance is None or isinstance(
                    provenance, UVManagedPyImplProvenance
                ), "Provenance must be a UVManagedPyImplProvenance"
                if plan in self._plan_to_group:
                    continue  # Already prepared

                if provenance is not None:
                    # Check for exact provenance match in existing groups
                    matched = False
                    for group in self._groups:
                        if group.provenance == provenance:
                            group.plans.append(plan)
                            self._plan_to_group[plan] = group
                            matched = True
                            break
                    if matched:
                        continue

                # Try to find existing group where plan requirements are strict subset
                assigned = False
                if provenance is None:  # Only try subset matching for non-provenance plans
                    plan_reqs = set(req.strip() for req in plan.requires.split("\n") if req.strip())
                    for group in self._groups:
                        if group.provenance is None:
                            continue  # Skip groups without finalized provenance
                        group_reqs = set(group.requirements)
                        if plan_reqs.issubset(group_reqs):
                            group.plans.append(plan)
                            self._plan_to_group[plan] = group
                            assigned = True
                            break

                if not assigned:
                    remaining_plans.append((plan, provenance))

            # Step 2: Partition and instantiate remaining plans using existing logic
            if remaining_plans:
                new_groups = await self._partition_plans(remaining_plans)
                new_plan_to_group = await self._instantiate_groups(new_groups)

                # Merge into main data structures
                self._groups.extend(new_groups)
                self._plan_to_group.update(new_plan_to_group)

    async def provide(
        self,
        plan: core.Plan[Any],
        dependencies: Mapping[core.Plan[Any], core.FxtrCallTarget],
        provenance_lock: core.AnyEntity | None = None,
    ) -> tuple[core.FxtrCallTarget, core.AnyEntity]:
        """Provide implementation of a single protocol implementation plan."""

        if not isinstance(plan, PyImplPlan):
            raise provider_base.ProviderUnsupportedPlanError(
                f"UVManagedPyImplProvider only supports PyImplPlan objects, got {type(plan)}"
            )

        # Lazily prepare the plan if it hasn't been prepared yet
        if plan not in self._plan_to_group:
            provenance_to_prepare = None
            if isinstance(provenance_lock, UVManagedPyImplProvenance):
                provenance_to_prepare = provenance_lock
            await self.prepare([(plan, provenance_to_prepare)])

        # Get the group and verify it has been instantiated
        group = self._plan_to_group[plan]
        assert group.spawner is not None, "Group should have spawner after prepare()"
        assert group.provenance is not None, "Group should have provenance after prepare()"

        # Check provenance lock if provided
        if provenance_lock is not None:
            if not isinstance(provenance_lock, UVManagedPyImplProvenance):
                raise provider_base.ProviderUnsupportedProvenanceLockError(
                    "UVManagedPyImplProvider only supports UVManagedPyImplProvenance for"
                    f" provenance locking, got {type(provenance_lock)}"
                )
            if provenance_lock != group.provenance:
                raise provider_base.InvalidPlanError(
                    "Provenance lock does not match stored provenance for this plan"
                )

        # Convert dependencies to plan-impl pairs
        dep_plan_items: list[DependencyPlanItem] = []
        for dep_plan in plan.impl_deps:
            dep_impl = dependencies[dep_plan]
            dep_plan_items.append({"plan": dep_plan, "impl": dep_impl})

        # Call the spawner to instantiate the implementation
        impl = await group.spawner.spawn(plan, dep_plan_items)

        return impl, group.provenance

    async def _partition_plans(
        self,
        plan_provenance_pairs: Sequence[tuple[PyImplPlan, UVManagedPyImplProvenance | None]],
    ) -> list[_PlanGroup]:
        """Partition plans into groups without checking dependencies.

        This uses the partitioning logic from the requirements: first group by
        identical provenance, then iteratively group remaining plans by requirements compatibility.
        """
        input_plans = list(plan_provenance_pairs)
        groups = []

        # First, group plans with identical locked provenance
        provenance_groups = {}
        remaining_plans = []

        for plan, provenance in input_plans:
            if provenance is not None:
                # Group by provenance identity
                if provenance not in provenance_groups:
                    provenance_groups[provenance] = []
                provenance_groups[provenance].append(plan)
            else:
                remaining_plans.append(plan)

        # Create groups for each unique provenance
        for provenance, plans_list in provenance_groups.items():
            group_id = str(uuid.uuid4())
            virtual_env_path = self._virtual_env_dir / group_id
            group = _PlanGroup(
                plans=plans_list,
                virtual_env_path=virtual_env_path,
                provenance=provenance,
                python_version=self._default_python_version,
            )
            # Set up locked environment
            await self._setup_locked_environment(group)
            groups.append(group)

        # Now iteratively group the remaining plans by requirements compatibility
        while remaining_plans:
            # Take the first plan and create a new group
            plan = remaining_plans.pop(0)
            group_id = str(uuid.uuid4())
            virtual_env_path = self._virtual_env_dir / group_id

            group = _PlanGroup(
                plans=[plan],
                virtual_env_path=virtual_env_path,
                provenance=None,
                python_version=self._default_python_version,
            )

            # Set up initial environment
            await self._setup_initial_environment(group)

            # Try to add compatible plans
            await self._add_compatible_plans_simplified(group, remaining_plans)
            await self._add_exact_requirements_plans_simplified(group, remaining_plans)

            # Finalize environment and create provenance
            await self._finalize_new_environment(group)
            groups.append(group)

        return groups

    async def _instantiate_groups(
        self, groups: Sequence[_PlanGroup]
    ) -> dict[PyImplPlan, _PlanGroup]:
        """Instantiate spawner processes for each group and return plan -> group mapping."""
        result = {}

        async def _instantiate(group: _PlanGroup):
            spawner_url = await self._instantiate_group_spawner(group)
            # Create RemoteImpl for the spawner
            group.spawner = impl_remote.RemoteImpl(
                task_group=self._task_group,
                interface_spec=core.spec_of(_UV_SPAWNER_PROTOCOL),
                base_url=spawner_url,
                shutdown_event=self._shutdown_event,
            )
            for plan in group.plans:
                result[plan] = group

        async with anyio.create_task_group() as tg:
            for group in groups:
                tg.start_soon(_instantiate, group)

        for group in groups:
            for plan in group.plans:
                assert plan in result

        return result

    async def _instantiate_group_spawner(self, group: _PlanGroup) -> str:
        """Instantiate a spawner process for a group and return the spawner URL."""

        # Create a random token for this group
        token = str(uuid.uuid4())

        # Create temporary local impl to receive spawner registration
        register_handler = _UVRegisterServerImpl(token)

        # Serve the register impl temporarily
        with self._impl_server.serve_impl(register_handler) as register_url:
            # Run the entrypoint subprocess
            cmd = [
                "uv",
                "run",
                "python",
                "-m",
                "fxtr.providers.uv_py_provider_entrypoint",
                register_url,
                token,
            ]

            logger.info(f"Running spawner command: {cmd} in {group.virtual_env_path}")
            # Monitor the process for unexpected exits and terminate when shutdown event is set
            error_process_send, error_process_recv = anyio.create_memory_object_stream(1)
            self._task_group.start_soon(
                self._manage_uv_process,
                cmd,
                group.virtual_env_path,
                self._shutdown_event,
                error_process_send,
            )

            # Wait for the spawner to register itself or the process to exit
            async with error_process_recv:
                race_winner, race_value = await _race_events(
                    spawner_registered=register_handler._received_event.wait,
                    uv_process_exited=error_process_recv.receive,
                )
            if race_winner == "uv_process_exited":
                assert isinstance(race_value, anyio.abc.Process)
                raise RuntimeError(
                    f"UV process failed to set up spawner! Exit code: {race_value.returncode}"
                )
            assert race_winner == "spawner_registered"
            spawner_url = register_handler._spawner_url
            assert spawner_url is not None

        return spawner_url

    # Environment setup methods (simplified versions without dependency checking)

    async def _setup_locked_environment(self, group: _PlanGroup) -> None:
        """Set up virtual environment using locked provenance."""

        # Create the virtual environment directory
        group.virtual_env_path.mkdir(parents=True, exist_ok=True)

        # Create pyproject.toml with exact requirements from provenance
        assert group.provenance is not None
        pyproject_path = group.virtual_env_path / "pyproject.toml"
        pyproject_content = self._create_pyproject_toml(
            group.provenance.requirements, group.python_version
        )
        pyproject_path.write_text(pyproject_content)

        # Create uv.lock file from provenance
        uv_lock_path = group.virtual_env_path / "uv.lock"
        uv_lock_path.write_text(group.provenance.uv_lock)

        # Run uv sync --locked
        await self._run_uv_command(["sync", "--locked"], cwd=group.virtual_env_path)

    async def _setup_initial_environment(self, group: _PlanGroup) -> None:
        """Set up initial virtual environment with just the first plan's requirements."""

        # Create the virtual environment directory
        group.virtual_env_path.mkdir(parents=True, exist_ok=True)

        # Start with just the first plan's requirements
        assert len(group.plans) == 1, "Initial environment should have exactly one plan"
        plan = group.plans[0]
        assert isinstance(plan, PyImplPlan)

        # Collect requirements from the first plan
        all_requirements = ["fxtr>=0.0.1"]  # Always include behaviors package
        plan_reqs = [req.strip() for req in plan.requires.split("\n") if req.strip()]
        all_requirements.extend(plan_reqs)

        # Store requirements in the group
        group.requirements = all_requirements

        # Create pyproject.toml
        pyproject_path = group.virtual_env_path / "pyproject.toml"
        pyproject_content = self._create_pyproject_toml(all_requirements, group.python_version)
        pyproject_path.write_text(pyproject_content)

        # Run uv lock to resolve dependencies (but don't sync yet)
        await self._run_uv_command(["lock"], cwd=group.virtual_env_path)

    async def _add_compatible_plans_simplified(
        self,
        group: _PlanGroup,
        remaining_plans: list[PyImplPlan],
    ) -> None:
        """Try to add compatible plans without locked provenance to the group."""

        for plan in list(remaining_plans):
            # Try adding this plan to the environment
            if await self._try_add_plan_to_environment(group, plan):
                remaining_plans.remove(plan)
            else:
                # If it fails, break out of the loop as per spec
                break

    async def _add_exact_requirements_plans_simplified(
        self,
        group: _PlanGroup,
        remaining_plans: list[PyImplPlan],
    ) -> None:
        """Add plans whose requirements are a subset of the group's existing requirements."""

        # Get the combined set of all requirements from all plans in the group
        group_requirements = set()
        for existing_plan in group.plans:
            existing_reqs = set(
                req.strip() for req in existing_plan.requires.split("\n") if req.strip()
            )
            group_requirements.update(existing_reqs)

        to_remove = []
        for plan in remaining_plans:
            # Get this plan's requirements
            plan_reqs = set(req.strip() for req in plan.requires.split("\n") if req.strip())

            # Check if this plan's requirements are a subset of the group's requirements
            if plan_reqs.issubset(group_requirements):
                # Add this plan to the group
                group.plans.append(plan)
                to_remove.append(plan)

        # Remove all added plans from remaining_plans
        for plan in to_remove:
            remaining_plans.remove(plan)

    async def _try_add_plan_to_environment(self, group: _PlanGroup, plan: PyImplPlan) -> bool:
        """Try adding a plan's requirements to the environment by testing compatibility."""

        # Get current requirements from the group
        current_deps = group.requirements.copy()
        current_deps_set = set(current_deps)

        # Add new requirements from this plan
        plan_reqs = []
        for req in plan.requires.split("\n"):
            req = req.strip()
            if req and req not in current_deps_set:
                plan_reqs.append(req)
        if not plan_reqs:
            return True
        new_deps = current_deps + plan_reqs

        # Create new pyproject.toml content
        new_content = self._create_pyproject_toml(new_deps, group.python_version)

        # Back up both pyproject.toml and uv.lock
        pyproject_path = group.virtual_env_path / "pyproject.toml"
        backup_pyproject_content = self._create_pyproject_toml(current_deps, group.python_version)
        uv_lock_path = group.virtual_env_path / "uv.lock"
        assert uv_lock_path.exists()
        backup_uv_lock_content = uv_lock_path.read_text()

        try:
            pyproject_path.write_text(new_content)
            await self._run_uv_command(["lock"], cwd=group.virtual_env_path)
            # Update the group's requirements on success
            group.requirements = new_deps
            group.plans.append(plan)
            return True
        except Exception:
            # Restore original content on failure
            pyproject_path.write_text(backup_pyproject_content)
            uv_lock_path.write_text(backup_uv_lock_content)
            return False

    async def _finalize_new_environment(self, group: _PlanGroup) -> None:
        """Finalize environment setup after all compatible plans have been added."""

        # Run uv sync --locked to install all dependencies
        await self._run_uv_command(["sync", "--locked"], cwd=group.virtual_env_path)

        # Create provenance record by reading the final lockfile
        uv_lock_path = group.virtual_env_path / "uv.lock"
        uv_lock_content = uv_lock_path.read_text()

        group.provenance = UVManagedPyImplProvenance(
            python_version=group.python_version,
            requirements=tuple(group.requirements),
            uv_lock=uv_lock_content,
        )

    def _create_pyproject_toml(self, requirements: Sequence[str], python_version: str) -> str:
        """Create a pyproject.toml file content with the given requirements."""

        doc = tomlkit.document()

        # Add project metadata
        project = tomlkit.table()
        project["name"] = "uv-managed-impl"
        project["version"] = "0.0.1"
        project["requires-python"] = f"=={python_version}"
        project["dependencies"] = list(requirements)
        doc["project"] = project

        # Add UV configuration if provided
        if self._uv_config:
            tool = doc.setdefault("tool", tomlkit.table())
            tool["uv"] = self._uv_config

        return tomlkit.dumps(doc)

    async def _run_uv_command(self, args: list[str], cwd: pathlib.Path) -> None:
        """Run a UV command and register it for cleanup."""

        cmd = ["uv"] + args

        process = await anyio.open_process(cmd, cwd=cwd, stdout=None, stderr=None)
        await process.wait()
        if process.returncode != 0:
            raise RuntimeError(f"UV command failed: {' '.join(cmd)}")

    async def _manage_uv_process(
        self,
        cmd: list[str],
        cwd: pathlib.Path,
        shutdown_trigger: anyio.Event,
        error_stream: anyio.streams.memory.MemoryObjectSendStream[Any],
    ) -> None:
        """Manage a UV process and its cleanup."""

        async with error_stream:
            async with await anyio.open_process(
                cmd, cwd=cwd, stdout=None, stderr=None, start_new_session=True
            ) as process:
                logger.info("UV process PID: %s", process.pid)
                try:
                    async with anyio.create_task_group() as tg:

                        async def _wait_for_shutdown():
                            await shutdown_trigger.wait()
                            tg.cancel_scope.cancel()

                        async def _wait_for_process():
                            await process.wait()
                            tg.cancel_scope.cancel()

                        tg.start_soon(_wait_for_shutdown)
                        tg.start_soon(_wait_for_process)
                finally:
                    if process.returncode is None:
                        logger.info(f"Sending SIGTERM to UV process {process.pid}")
                        process.send_signal(signal.SIGTERM)
                        with anyio.move_on_after(1000, shield=True):
                            await process.wait()
                        if process.returncode is None:
                            logger.warning(f"Killing unresponsive worker process {process.pid} and all subprocesses")
                            os.killpg(process.pid, signal.SIGKILL)
                            # process.kill()
                            if _FAIL_ON_KILL.get():
                                raise RuntimeError(
                                    f"Worker process {process.pid} did not shutdown in time!"
                                    " Failing as requested"
                                )

            if process.returncode != 0 and not shutdown_trigger.is_set():
                try:
                    await error_stream.send(process)
                except anyio.BrokenResourceError:
                    raise RuntimeError(
                        f"UV process unexpectedly exited with return code {process.returncode}"
                    )


@contextlib.asynccontextmanager
async def running_uv_managed_provider(
    virtual_env_dir: pathlib.Path,
    impl_server: ImplServer | None = None,
    plan_provenance_pairs: (
        Sequence[tuple[PyImplPlan, UVManagedPyImplProvenance | None]] | None
    ) = None,
    uv_config: dict | None = None,
    remove_env_on_exit: bool = True,
    python_version: str = _DEFAULT_PY_VERSION,
) -> AsyncGenerator[UVManagedPyImplProvider, None]:
    """Create a UV-managed provider that can dynamically instantiate implementations.

    This is the main entry point for using UV-managed providers. The provider can be used
    in two ways:
    1. Pre-configure plans by passing plan_provenance_pairs (optional)
    2. Dynamically prepare plans on-demand via provider.prepare() or lazily during provide()

    Args:
        virtual_env_dir: Directory where virtual environments will be created
        impl_server: Server for hosting implementations. If None, a new server will be created
        plan_provenance_pairs: Optional sequence of (plan, provenance) pairs to prepare upfront
        uv_config: Optional UV configuration to include in pyproject.toml
        remove_env_on_exit: Whether to remove environments on cleanup
        python_version: Python version constraint used for all environments
    """
    shutdown_event = anyio.Event()
    async with contextlib.AsyncExitStack() as stack:
        tg = await stack.enter_async_context(anyio.create_task_group())
        if impl_server is None:
            impl_server = await stack.enter_async_context(ImplServer().serving())

        # Create the provider
        provider = UVManagedPyImplProvider(
            virtual_env_dir=virtual_env_dir,
            impl_server=impl_server,
            task_group=tg,
            shutdown_event=shutdown_event,
            uv_config=uv_config,
            remove_env_on_exit=remove_env_on_exit,
            python_version=python_version,
        )

        # Optionally prepare plans upfront
        if plan_provenance_pairs is not None:
            await provider.prepare(plan_provenance_pairs)

        try:
            yield provider
        finally:
            shutdown_event.set()


class _UVRegisterServerImpl(_UV_REGISTER_PROTOCOL):
    """Implementation object for receiving spawner registration."""

    interface_spec = core.INFER_INTERFACE_FROM_BASE_CLASS

    def __init__(self, expected_token: str):
        self._expected_token = expected_token
        self._spawner_url: str | None = None
        self._received_event = anyio.Event()
        self._token_received = False

    async def register(self, token: str, spawner_url: str) -> None:
        """Register a spawner URL with token validation."""
        # Set flag first as per requirements, then validate
        self._token_received = True

        if token != self._expected_token:
            self._received_event.set()
            raise RuntimeError(
                f"Invalid token received: expected {self._expected_token}, got {token}"
            )

        self._spawner_url = spawner_url
        self._received_event.set()
