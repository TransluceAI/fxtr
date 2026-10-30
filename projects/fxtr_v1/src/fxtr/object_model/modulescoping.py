"""Utility for tracking module-level collection scopes.

When entities are defined as part of a Python module, at the top level, we often want to track the
collection source in order to associate the entities with their locations in their modules.

This can be done explicitly, by running the full module code in a context manager. However, this
can be jarring and adds an extra indent level to the whole module, which can feel unnatural.
Instead, it would be nice if it was possible to register a context so that code being executed
while the module is being defined would automatically be associated with the collection. We might
in particular want it to be possible to do something like

```
some_library.enter_module_scope(id="foo")

some_library.active_scope()  # returns "foo" while _this module_ is the innermost active one
```

This helper module provides tooling to do this, by making use of Python traceback inspection.
In particular, to tell which frames are module-level execution frames, we can walk up the stack
and look for frames where `frame.f_globals is frame.f_locals`, which is only true if we are actually
executing directly in the global scope.

A slight complication is that we do not ever get a notification when a module is done executing
(whereas we _would_ get an __exit__ notification after the context manager is exited). So, each
time we query the active scope, we need to check if the module is still executing.

Efficiency is somewhat less of a concern, because this tracking mode should only be used during
import time, never while logic is also running. Also, this only needs to happen when we are
building collections for fxtr introspection (e.g. `fxtr ls`). (Right now it happens all the time,
but we could disable this in the future.)

To make this slightly more efficient, however, we follow the following overall design:
- This module maintains two pieces of state:
    - An active "local" context var assignments
    - A stack of "module-level" active assignments, keyed by name
- Contexts are only allowed to nest in specific ways:
    - Local assignments can be nested inside each other. In a local assigment context, you always
      get the innermost local value.
    - New local assignments can override existing local assignments, and can also override
      module-level assignments.
    - However, module-level assignments CANNOT override local assignments; it is an error to start
      a module-level assignment context while a local assignment context is active. Usually this
      only happens if you try to import a module while a local assignment context is active. You
      can disable this manually if you absolutely need to.
    - Additionally, module-level assignments ONLY look at the innermost module frame; values from
      one module frames are not visible to other module framess.
- When retrieving the active assignment:
    - If we are in a local context, we return that
    - If not, if we think we might be in a module context, we walk up the stack and collect module
      frames.
        - If the innermost module frame is one we know, return its assignment
        - Otherwise, there is no active module frame. But we also want to detect if any module
          frames are closed. Continue walking up until we recognize a model frame or run out of
          frames, and drop any frames that we didn't see in between.
"""

from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import inspect
import logging
import sys
import types
import warnings
from typing import Iterator

logger = logging.getLogger(__name__)


def get_module_name_from_frame(frame: types.FrameType) -> str | None:
    """Check if a frame is a module frame, and return its name if so."""
    frame_name = frame.f_code.co_name
    if frame_name != "<module>":
        return None

    caller_globals = frame.f_globals
    module_name = caller_globals.get("__name__")
    if module_name is None:
        return None

    if module_name not in sys.modules:
        warnings.warn(
            "Tried to extract a module name for a module frame, but the stack frame's"
            f" reported name {repr(module_name)} is not in sys.modules",
            stacklevel=2,
        )
        return None

    if sys.modules[module_name].__dict__ is not caller_globals:
        warnings.warn(
            "Tried to extract a module name for a module frame, but the stack frame's"
            f" reported name {repr(module_name)} does not match the module with that"
            " name in sys.modules",
            stacklevel=2,
        )
        return None

    return module_name


def find_module_name_from_frame_at_stacklevel(stacklevel: int) -> str | None:
    """Find the name of the module frame, the given number of stack frames up.

    Returns None if the specified frame is not a stack frame (usually the caller should raise an
    exception in this case).

    Args:
        stacklevel: The stack level from the module scope to find the frame name at. 1 means
            the caller of find_module_name_from_frame_at_stacklevel, 2 means the caller of that
            caller, etc.

    Returns:
        The name of the module frame at the given stack depth, if known.
    """
    try:
        frame = inspect.currentframe()
        if frame is None:
            return None
        for _ in range(stacklevel):
            frame = frame.f_back
            if frame is None:
                return None

        return get_module_name_from_frame(frame)

    except Exception as e:
        warnings.warn(
            f"Error while trying to determine module name where this call occurred: {e}",
            stacklevel=1 + stacklevel,
        )
        return None


class ModuleScopedAssignmentError(RuntimeError):
    """Raised when an error occurs with a module-scoped assignment."""


@dataclasses.dataclass
class ModuleScopedAssignment[T]:
    """A module-scoped assignment variable.

    All attributes of this should be considered private to this object.

    Attributes:
        _name: The name of this assignment variable.
        _local_state: The context variable that stores the local assignment, if any.
        _active_module_stack: A tuple of (a) the stack of active module frames and (b) a mapping
            from module names to the index of the module in the stack.
            If empty, we are not tracking any module frames (in this thread). If it exists, it
            should include the name of the first module frame we are tracking, followed by the name
            of _every_ other module frame we have encountered between the first-tracked frame and
            the most recent time this assigment was accessed.
        _active_module_lookup: A
        _module_states: A mapping from module names to the assignment value at that module level.
            Will include a value for every module in _active_module_stack.
    """

    _name: str
    _local_state: contextvars.ContextVar[T | None]
    _active_module_stack_state: contextvars.ContextVar[tuple[list[str], dict[str, int]]]
    _module_states: dict[str, T | None]

    @classmethod
    def create(cls, name: str) -> ModuleScopedAssignment[T]:
        return cls(
            _name=name,
            _local_state=contextvars.ContextVar(f"{name}.local_state", default=None),
            _active_module_stack_state=contextvars.ContextVar(f"{name}._active_module_stack"),
            _module_states={},
        )

    @contextlib.contextmanager
    def _scoped_assign_or_clear(self, value: T | None) -> Iterator[None]:
        reset_token = self._local_state.set(value)
        try:
            yield
        finally:
            state_after = self._local_state.get()
            if state_after is not value:
                raise RuntimeError(
                    "Restoration error: could not restore local assignment of"
                    f" ModuleScopedAssignment {repr(self._name)}; value unexpectedly changed from"
                    f" {repr(value)} to {repr(state_after)}"
                )
            self._local_state.reset(reset_token)

    @contextlib.contextmanager
    def locally_scoped_assign(self, value: T) -> Iterator[None]:
        """Assign a value to the variable for the duration of the context.

        Note that local assignments override module-level assignments, and it is not allowed to
        start a module-level assignment unless the current local assignment is cleared.
        """
        if value is None:
            raise ValueError("Cannot assign None to a local")
        with self._scoped_assign_or_clear(value):
            yield

    @contextlib.contextmanager
    def clear_locally_scoped_assignment(self) -> Iterator[None]:
        """Clear the local assignment for the duration of the context.

        While the local scoped assignment is cleared, module-level assignments will be used if
        available.
        """
        with self._scoped_assign_or_clear(None):
            yield

    def _update_active_module_stack(self, ensure_active: str | None = None) -> list[str]:
        """Updates and returns the current module stack, to include all descendants of the first.

        This function checks and updates the `_active_module_stack` variable. It does so by walking
        frames until it finds a module frame that it recognizes `_active_module_stack`, then
        replacing the frames afterward with the new frames.

        Args:
            ensure_active: If not None, the name of the module to ensure is active. If this module
                is not the active module, we will raise an error. If no other modules are being
                tracked, sets it so that this one is tracked. (When this is None, we may instead
                clear the active module stack.)

        Returns:
            The updated module stack, which will start with some tracked active module, and include
            every other module. If `ensure_active` is provided, the updated module stack will
            always end in `ensure_active` (or an exception will be raised).

        Raises:
            ModuleScopedAssignmentError: If `ensure_active` is provided and the active module
                stack does not end in `ensure_active`.
        """
        cur_stack_state = self._active_module_stack_state.get(None)
        if cur_stack_state is None:
            active_stack = []
            active_lookup = {}
            self._active_module_stack_state.set((active_stack, active_lookup))
        else:
            active_stack, active_lookup = cur_stack_state

        if not active_stack and not ensure_active:
            # We aren't tracking any module state, so we don't need to return anything.
            return active_stack

        logger.debug(f"Starting stack walk with {ensure_active=}")

        frame = inspect.currentframe()
        if frame is None:
            raise ModuleScopedAssignmentError("Couldn't infer current stack frame!")

        # Track modules we've seen but aren't already tracking, as we walk up the stack.
        newly_seen_modules = []

        while frame is not None:
            name = get_module_name_from_frame(frame)

            if name is not None:
                logger.debug(f"  Module stack walk: saw {repr(name)}")
                old_ix = active_lookup.get(name)
                if old_ix is None:
                    newly_seen_modules.append(name)
                else:
                    logger.debug(f"  Matched existing stack at {old_ix}.")
                    logger.debug(f"    Keeping {active_stack[:old_ix+1]}")
                    logger.debug(f"    Replacing {active_stack[old_ix+1:]}")
                    logger.debug(f"      with {newly_seen_modules[::-1]}")
                    # Saw a tracked module!
                    # Remove stale old module names.
                    for old_to_remove in active_stack[old_ix + 1 :]:
                        del self._module_states[old_to_remove]
                        del active_lookup[old_to_remove]
                    del active_stack[old_ix + 1 :]
                    # Add new module names.
                    new_ix = len(active_stack)
                    for new_to_add in newly_seen_modules[::-1]:
                        active_stack.append(new_to_add)
                        active_lookup[new_to_add] = new_ix
                        self._module_states[new_to_add] = None
                        new_ix += 1
                    # Make sure the requested module is active.
                    if ensure_active is not None:
                        assert active_stack
                        if active_stack[-1] != ensure_active:
                            raise ModuleScopedAssignmentError(
                                f"Tried to assign a module-level value for module {ensure_active},"
                                f" but the active module is {active_stack[-1]}"
                            )
                    logger.debug(f"    => stack is now {active_stack}")
                    return active_stack

            frame = frame.f_back

        # Made it to the top of the stack without finding any of the module names we were tracking.
        # Clear all of our tracked modules.
        logger.debug(f"  No match to existing stack.")
        logger.debug(f"    Dropping {active_stack}")
        for old_to_remove in active_stack:
            del self._module_states[old_to_remove]
            del active_lookup[old_to_remove]
        del active_stack[:]

        if ensure_active is not None:
            if not newly_seen_modules:
                raise ModuleScopedAssignmentError(
                    f"Tried to assign a module-level value for module {ensure_active}, but no"
                    " modules were found at all during stack walk (this should be impossible?)"
                )
            elif ensure_active == newly_seen_modules[0]:
                # No other tracked modules, but this one is active, so start tracking it. We don't
                # need to track any of the other stack frames we saw.
                active_stack.append(ensure_active)
                active_lookup[ensure_active] = 0
                self._module_states[ensure_active] = None
                logger.debug(f"    Setting tracked stack to just {active_stack}")
            else:
                raise ModuleScopedAssignmentError(
                    f"Tried to assign a module-level value for module {ensure_active}, but the"
                    f" active module is {active_stack[-1]}"
                )

        logger.debug(f"    => stack is now {active_stack}")
        return active_stack

    def module_level_assign(self, module_name: str, value: T | None):
        """Assign a value to the variable at the module level.

        When this function is called, the named module must currently be executing and must have a
        stack frame. Usually, the given module name should come from `find_module_name_from_frame_at_stacklevel`.

        This function can only be called while there is no local assignment active, and if the
        module's stack frame is the current active one.

        Args:
            module_name: The name of the module to assign the value to.
            value: The value to assign to the variable.
        """
        if self._local_state.get() is not None:
            raise ModuleScopedAssignmentError(
                "Cannot assign a module-level value while a local assignment is active! If you are"
                " importing a module with module-level assignments inside a local assignment scope,"
                " you must locally clear the variable using `scoped_clear`."
            )
        module_stack = self._update_active_module_stack(ensure_active=module_name)
        assert module_stack and module_stack[-1] == module_name
        self._module_states[module_name] = value

    def get(self) -> T | None:
        """Get the current value of the variable.

        If a local scope is active

        Returns:
            The current value of the variable, or None if no value is assigned.
        """
        local_value = self._local_state.get()
        if local_value is not None:
            return local_value

        module_stack = self._update_active_module_stack()
        if module_stack:
            return self._module_states[module_stack[-1]]

        return None

    def is_tracked_module_executing(self, module_name: str) -> bool:
        """Check if a tracked module is currently executing.

        Once an assignment has been made for a module, that module remains tracked until that
        module's frame finishes execution. This function can be used to determine whether this has
        occurred.

        The primary use for this is to do some sort of finishing-up when values from the module are
        used from outside it.

        Args:
            module_name: A module to check for having finished execution.

        Returns:
            True if both (a) this module is being tracked (usually because some assignment was
            made inside it), and (b) it is still executing.
        """
        module_stack = self._update_active_module_stack()
        return module_name in module_stack
