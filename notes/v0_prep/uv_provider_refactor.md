# Current state

/Users/daniel/clarity/lib/behaviors/behaviors/staging/providers/uv_py_provider.py contains a provider, which you use by first opening a scope where it is running and also passing plans. It then partitions and launches those plans using _UVManagedPyImplHelper, and returns a new UVManagedPyImplProvider object that can be used to instantiate the objects from their spawners.

# Desired new state

There should just be the UVManagedPyImplProvider object, which should subsume the functionality of the _UVManagedPyImplHelper into itself. It should expose two methods: `prepare`, which takes `plan_provenance_pairs` and prepares their runners, and `provide` (which already exists) which will provide the actual implementation.

You should still use `running_uv_managed_provider` to spawn it, but `running_uv_managed_provider` should have its `plan_provenance_pairs` be optional. It should create the UVManagedPyImplProvider and manage the shutdown event, and it should just forward the `plan_provenance_pairs` to `prepare` if provided.

When you call `prepare` on the UVManagedPyImplProvider, it should go through the pairs and see if it can greedily assign them to running groups (only if their requirements are a strict subset of the existing group). Then it should partition and instantiate the remaining groups using the current logic.

When you call `provide`, if the plan is not already in its spawner dict, it should first `prepare` it alone. Then it should look up the spawner and instantiate it the way that it currently does. It should probably store the plan groups instead of just the _plan_to_spawner dict so that it can recover the requirements, spawners, and provenance for existing groups.


# Implementation plan

## Phase 1: Extend _PlanGroup dataclass
- Add `spawner_url: str | None` field to _PlanGroup
- This will track the spawner URL once a group is instantiated

## Phase 2: Refactor UVManagedPyImplProvider
- Move all helper methods from _UVManagedPyImplHelper into UVManagedPyImplProvider
- Add instance variables:
  - `_groups: list[_PlanGroup]` - list of all plan groups
  - `_plan_to_group: dict[PyImplPlan, _PlanGroup]` - quick lookup from plan to its group
  - `_prepare_lock: anyio.Lock` - ensures sequential prepare() calls
- Remove `_plan_to_spawner` dict (replaced by _plan_to_group + group.spawner_url)

## Phase 3: Implement prepare() method
```python
async def prepare(
    self,
    plan_provenance_pairs: Sequence[tuple[PyImplPlan, UVManagedPyImplProvenance | None]]
) -> None:
    async with self._prepare_lock:
        # Step 1: Try greedy assignment to existing groups
        remaining_plans = []
        for plan, provenance in plan_provenance_pairs:
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
```

## Phase 4: Update provide() method
- Change to check if plan is in `_plan_to_group`
- If not, call `await self.prepare([(plan, provenance_lock)])`
- Then proceed with spawning using `self._plan_to_group[plan]`

## Phase 5: Refactor _partition_plans and _instantiate_groups
- Rename from partition_plans/instantiate_groups (remove "helper" context)
- Make them return appropriate data structures:
  - `_partition_plans()` returns `list[_PlanGroup]`
  - `_instantiate_groups()` returns `dict[PyImplPlan, _PlanGroup]` and sets spawner_url on groups

## Phase 6: Update running_uv_managed_provider
- Make `plan_provenance_pairs` optional
- Create UVManagedPyImplProvider immediately with empty state
- If `plan_provenance_pairs` provided, call `await provider.prepare(plan_provenance_pairs)`
- Simplify: just manage lifecycle (task group, impl server, shutdown event)

## Phase 7: Update __init__ signature
- Constructor takes: virtual_env_dir, impl_server, task_group, shutdown_event, uv_config, etc.
- Initialize empty groups list, plan_to_group dict, and prepare_lock
- Remove _plan_to_spawner parameter

## Phase 8: Clean up
- Remove _UVManagedPyImplHelper class entirely
- Update all internal method calls to use self instead of helper
- Ensure all async context managers and cleanup work correctly

# Current status
Implementation complete! All phases have been successfully implemented:

1. ✅ Extended _PlanGroup dataclass with spawner_url field
2. ✅ Refactored UVManagedPyImplProvider with new instance variables (_groups, _plan_to_group, _prepare_lock)
3. ✅ Implemented prepare() method with greedy assignment logic
4. ✅ Updated provide() method for lazy instantiation
5. ✅ Moved all methods from _UVManagedPyImplHelper into UVManagedPyImplProvider
6. ✅ Simplified running_uv_managed_provider context manager with optional plan_provenance_pairs
7. ✅ Updated __init__ signature to take lifecycle parameters directly
8. ✅ Removed _UVManagedPyImplHelper class entirely

The provider now supports:
- Dynamic plan preparation via prepare() method
- Lazy instantiation during provide() if plan not yet prepared
- Greedy assignment to existing groups (exact provenance match or subset requirements)
- Thread-safe preparation with anyio.Lock
- Simplified API with optional upfront plan preparation
