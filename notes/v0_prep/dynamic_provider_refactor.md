# Dynamic provider refactor

Please read lib/behaviors/behaviors/staging/README.md for context.

# Current state

There are three entry points for managing and providing plans:

- lib/behaviors/behaviors/staging/providers/in_process_py_provider.py can provide some python objects in process
- lib/behaviors/behaviors/staging/providers/uv_py_provider.py can be used to provide python objects running in an isolated process
- lib/behaviors/behaviors/staging/api/task_launcher.py dispatches to uv_py_provider and also manually handles TaskResultHandle and ExternalServiceIdentifier for SecretsProtocol.

The API for creating implementations is not clearly defined, and task_launcher is the main entry point.

Only the uv provider supports `prepare`.

# Desired state

## Add `prepare` to ABC
The lib/behaviors/behaviors/staging/providers/provider_base.py API should support the `prepare` method. It should by default do nothing and you don't have to implement it if you want to inherit this behavior.

## Refactor out secrets providers
Secret providing logic in lib/behaviors/behaviors/staging/api/task_launcher.py should be moved to lib/behaviors/behaviors/staging/providers/externals_provider.py and imported from there as a provider class that takes a fxtr config and a interactive flag (defaulting to True).

## Partially refactor out run data providers
There should be a TaskResultHandle provider in lib/behaviors/behaviors/staging/providers/externals_provider.py which takes a fxtr config, and looks up existing runs of the tasks. It should have a `run_resolution_strategy` arg which defaults to "only_unique". This should be similar to how the task_launcher works, but the task_launcher shouldn't use this, because it needs to resolve runs for tasks itself.

## Add a local resolver module
There should be a file lib/behaviors/behaviors/staging/providers/auto_provider.py

In it there should be an object `AutoProvider` parameterized by a path to a fxtr config toml (see lib/behaviors/behaviors/staging/api/fxtrconfig.py) and a python provider object. The autoprovider should be a provider, and it should support both `prepare` and `provide` methods (similar to the UV provider).
- `prepare` isolates the python plans and sends them to the UV provider if it is in UV mode; it does nothing otherwise
- `provide` dispatches to an appropriate provider based on the type of the plan

There should be a context manager `with auto_provider(path_to_fxtr_config, python_provider: Literal["in_process", "uv"] = "in_process") as provider:` which spawns a UV provider or an in-process provider based on the args and the config settings (similar to how task_launcher does it) and tracks it in a contextlib.AsyncExitStack if needed, and yields an AutoProvider.

# Implementation plan

## Phase 1: Update provider_base.py ABC

**File**: `lib/behaviors/behaviors/staging/providers/provider_base.py`

Add a `prepare` method to the `ImplProvider` ABC:
```python
async def prepare(
    self,
    plan_provenance_pairs: Sequence[tuple[core.AnyProtocolImplPlan, core.AnyEntity | None]],
) -> None:
    """Prepare plans by pre-computing environments or validating dependencies.

    This method allows providers to batch-prepare multiple plans before they are needed.
    The default implementation does nothing, which is appropriate for providers that
    can instantiate plans on-demand without preparation overhead.

    Args:
        plan_provenance_pairs: Sequence of (plan, provenance) pairs to prepare.
    """
    pass  # Default no-op implementation
```

**Changes needed**:
- Add the abstract method with a default no-op implementation to `ImplProvider` class
- Import `Sequence` from typing if not already imported

## Phase 2: Create provider classes in externals_provider.py

**File**: `lib/behaviors/behaviors/staging/providers/externals_provider.py`

### 2a. SecretsProvider class

Create a provider that handles `ExternalServiceIdentifier` plans pointing to `SecretsProtocol`:

```python
class SecretsProvider(provider_base.ImplProvider):
    """Provider for secrets from fxtr configuration.

    Args:
        config: The fxtr configuration containing secret definitions
        interactive: Whether to allow interactive approval prompts (default: True)
    """

    def __init__(self, config: fxtrconfig.FxtrConfig, interactive: bool = True):
        self._config = config
        self._interactive = interactive
        self._env_vars = self._load_env_vars()

    def _load_env_vars(self) -> dict[str, str]:
        """Load environment variables from .env file if configured."""
        # Logic extracted from task_launcher.py lines 456-463
        ...

    async def provide(
        self,
        plan: core.AnyProtocolImplPlan,
        dependencies: Mapping[core.AnyProtocolImplPlan, provider_base.ImplRoute],
        provenance_lock: core.AnyEntity | None = None,
    ) -> tuple[provider_base.ImplRoute, core.AnyEntity]:
        """Provide implementation for a secret plan."""
        # Validate plan type
        if not isinstance(plan, core.ExternalServiceIdentifier):
            raise provider_base.ProviderUnsupportedPlanError(...)
        if plan.protocol_spec != core.SecretsProtocol:
            raise provider_base.ProviderUnsupportedPlanError(...)

        # Logic extracted from task_launcher.py build_provider_for_secret_plan
        # (lines 822-898) but adapted to use self._config and self._interactive
        ...
```

**Extract from task_launcher.py**:
- `_find_env_file` function (lines 901-909) → private method `_load_env_vars`
- `build_provider_for_secret_plan` function (lines 822-898) → `provide` method logic
- `_request_secret_approval` function (lines 912-953) → private method
- `_make_secrets_provider` function (lines 1010-1029) → integrate into `provide` method
- `SecretsRetriever` class (lines 812-819) → keep or move to externals_provider.py

### 2b. TaskResultHandleProvider class

Create a provider that handles `TaskResultHandle` plans:

```python
class TaskResultHandleProvider(provider_base.ImplProvider):
    """Provider for task result handles from run data.

    This provider looks up existing runs for tasks and provides access to their data.
    It does NOT execute new runs - it only retrieves data from completed runs.

    Args:
        config: The fxtr configuration
        run_resolution_strategy: Strategy for resolving task runs ("only_unique" or "use_last")
        explicit_run_deps: Optional mapping of task CIDs to specific run CIDs to use
    """

    def __init__(
        self,
        config: fxtrconfig.FxtrConfig,
        run_resolution_strategy: Literal["use_last", "only_unique"] = "only_unique",
        explicit_run_deps: Mapping[cbrrr.CID, cbrrr.CID] | None = None,
    ):
        self._config = config
        self._run_resolution_strategy = run_resolution_strategy
        self._explicit_run_deps = explicit_run_deps or {}

        # Set up locator and loader
        self._run_data_locator = locator.ClosedBinaryCBOROnDiskLocator(
            fxtrconfig.get_active_entity_store(config)
        )
        self._run_data_loader = locator.CachingEntityLoader.from_locator(
            self._run_data_locator
        )

    async def provide(
        self,
        plan: core.AnyProtocolImplPlan,
        dependencies: Mapping[core.AnyProtocolImplPlan, provider_base.ImplRoute],
        provenance_lock: core.AnyEntity | None = None,
    ) -> tuple[provider_base.ImplRoute, core.AnyEntity]:
        """Provide implementation for a task result handle."""
        if not isinstance(plan, core.TaskResultHandle):
            raise provider_base.ProviderUnsupportedPlanError(...)

        # Logic similar to task_launcher.py _make_run_data_retriever_provider
        # (lines 364-390) but without closure state dependencies
        # - Look up task_cid from plan.task_ref
        # - Check explicit_run_deps first
        # - Otherwise use run_resolution_strategy to find run
        # - Return LocalImpl with RunDataRetriever and RunProvenance
        ...
```

**Extract from task_launcher.py**:
- `_make_run_data_retriever_provider` logic (lines 364-390) → `provide` method
- `RunDataRetriever` class (lines 801-809) → keep or move to externals_provider.py

**Key differences from task_launcher version**:
- No dependency on `task_done` events (assumes runs already exist)
- No `run_cid_for_task_handle` mutable state
- Standalone class that can be used outside task_launcher context

## Phase 3: Create auto_provider.py

**File**: `lib/behaviors/behaviors/staging/providers/auto_provider.py`

### 3a. AutoProvider class

```python
class AutoProvider(provider_base.ImplProvider):
    """Auto-dispatching provider that routes plans to specialized providers.

    Args:
        config: The fxtr configuration
        python_provider: The provider to use for Python implementation plans
        interactive: Whether to allow interactive prompts (default: True)
    """

    def __init__(
        self,
        config: fxtrconfig.FxtrConfig,
        python_provider: provider_base.ImplProvider,
        interactive: bool = True,
    ):
        self._config = config
        self._python_provider = python_provider
        self._interactive = interactive

        # Create specialized providers
        self._secrets_provider = externals_provider.SecretsProvider(
            config, interactive=interactive
        )
        self._task_result_provider = externals_provider.TaskResultHandleProvider(
            config, run_resolution_strategy="only_unique"
        )

    async def prepare(
        self,
        plan_provenance_pairs: Sequence[tuple[core.AnyProtocolImplPlan, core.AnyEntity | None]],
    ) -> None:
        """Prepare plans by delegating to appropriate providers."""
        # Filter to Python plans
        py_plans = [
            (plan, prov) for plan, prov in plan_provenance_pairs
            if isinstance(plan, py_impl_plan.PyImplPlan)
        ]

        # Delegate to python provider if it supports prepare
        if py_plans and hasattr(self._python_provider, 'prepare'):
            await self._python_provider.prepare(py_plans)

    async def provide(
        self,
        plan: core.AnyProtocolImplPlan,
        dependencies: Mapping[core.AnyProtocolImplPlan, provider_base.ImplRoute],
        provenance_lock: core.AnyEntity | None = None,
    ) -> tuple[provider_base.ImplRoute, core.AnyEntity]:
        """Provide implementation by dispatching to the appropriate provider."""

        # Dispatch based on plan type
        if isinstance(plan, py_impl_plan.PyImplPlan):
            return await self._python_provider.provide(plan, dependencies, provenance_lock)

        elif isinstance(plan, core.ExternalServiceIdentifier):
            if plan.protocol_spec == core.SecretsProtocol:
                return await self._secrets_provider.provide(plan, dependencies, provenance_lock)
            else:
                raise provider_base.ProviderUnsupportedPlanError(
                    f"AutoProvider does not support ExternalServiceIdentifier for protocol: {plan.protocol_spec}"
                )

        elif isinstance(plan, core.TaskResultHandle):
            return await self._task_result_provider.provide(plan, dependencies, provenance_lock)

        else:
            raise provider_base.ProviderUnsupportedPlanError(
                f"AutoProvider does not support plan type: {type(plan).__name__}"
            )
```

### 3b. auto_provider context manager

```python
@contextlib.asynccontextmanager
async def auto_provider(
    config_path: str | pathlib.Path,
    python_provider: Literal["in_process", "uv"] = "in_process",
    interactive: bool = True,
) -> AsyncGenerator[AutoProvider, None]:
    """Create an AutoProvider with the specified configuration.

    Args:
        config_path: Path to fxtr.toml configuration file
        python_provider: Which Python provider to use ("in_process" or "uv")
        interactive: Whether to allow interactive prompts

    Yields:
        An AutoProvider instance ready to use

    Example:
        async with auto_provider("fxtr.toml", python_provider="uv") as provider:
            route, provenance = await provider.provide(my_plan, {})
    """
    # Load config
    config = fxtrconfig.parse_fxtr_config(config_path)

    async with contextlib.AsyncExitStack() as stack:
        # Create appropriate python provider
        if python_provider == "in_process":
            py_provider = in_process_py_provider.InProcessPyProvider()
        elif python_provider == "uv":
            # Set up UV provider similar to task_launcher.py lines 506-557
            # - Extract UV config from fxtr.toml
            # - Create virtual_env_dir
            # - Enter uv_py_provider.running_uv_managed_provider context
            virtual_env_dir = pathlib.Path(config.provider.uv.virtual_env_dir)

            # Build UV config dict (extract from task_launcher logic)
            uv_config_dict = dict(config.provider.uv.custom_uv_config)
            # ... (UV config building logic from task_launcher)

            py_provider = await stack.enter_async_context(
                uv_py_provider.running_uv_managed_provider(
                    virtual_env_dir=virtual_env_dir,
                    uv_config=uv_config_dict if uv_config_dict else None,
                    remove_env_on_exit=True,
                )
            )
        else:
            raise ValueError(f"Invalid python_provider: {python_provider}")

        # Create and yield AutoProvider
        auto_prov = AutoProvider(
            config=config,
            python_provider=py_provider,
            interactive=interactive,
        )

        yield auto_prov
```

## Phase 4: Testing and validation

**File**: `lib/behaviors/behaviors/staging/examples/demo_09_15/user/test_auto_provider.py`

Create a demo script (not a pytest test) that exercises the AutoProvider in a realistic context:
- Load the experiment configuration from the demo_09_15 example
- Use the `auto_provider` context manager with both "in_process" and "uv" modes
- Try providing some plans that exist in that experiment
- Verify that:
  1. `prepare` method can be called on providers
  2. SecretsProvider can load and provide secrets (if any in the demo)
  3. TaskResultHandleProvider can retrieve existing run data (if any exists)
  4. AutoProvider correctly dispatches to sub-providers
  5. Both "in_process" and "uv" python_provider modes work

The script should be a standalone async script that can be run directly, similar to other utilities in the examples directory. It should match the testing patterns already used in that directory.

**Note**: Details of how to structure the test script will be determined when implementing this phase, by examining the existing example structure.

## Summary of changes

**New files**:
- `lib/behaviors/behaviors/staging/providers/auto_provider.py` (~150-200 lines)

**Modified files**:
- `lib/behaviors/behaviors/staging/providers/provider_base.py` (+8 lines)
- `lib/behaviors/behaviors/staging/providers/externals_provider.py` (+200-250 lines)

**Code extraction from task_launcher.py**:
- Secret-related logic → SecretsProvider
- Run data retrieval logic → TaskResultHandleProvider
- UV config building → auto_provider context manager

**Backward compatibility**:
- task_launcher.py continues to work as-is (no breaking changes)
- New providers can be used independently in notebooks/scripts

# Current status

## Completed
- ✅ Phase 1: Added `prepare` method to provider_base.py ABC
- ✅ Phase 2: Created SecretsProvider and TaskResultHandleProvider in externals_provider.py
- ✅ Phase 3: Created auto_provider.py with AutoProvider class and context manager
- ✅ Refactored task_launcher.py to use SecretsProvider instead of inline logic
- ✅ Phase 4: Created test script at `examples/demo_09_15/user/test_auto_provider.py`

## Implementation Complete! 🎉

The refactor is complete and ready to use. All providers have been implemented and tested.

## Notes
- task_launcher.py now uses externals_provider.SecretsProvider
- RunDataRetriever and SecretsRetriever classes moved to externals_provider.py
- Helper functions (_find_env_file, _request_secret_approval) moved to externals_provider.py
- _extract_uv_source_from_package remains in both task_launcher.py and auto_provider.py (duplication noted)
- Protocol specs fixed to use `core.protocol_spec()` function for proper type handling
- Test script demonstrates AutoProvider usage with both "in_process" and "uv" modes
