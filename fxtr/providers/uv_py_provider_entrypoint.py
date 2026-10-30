#!/usr/bin/env python3
"""Entrypoint script for UV-managed Python implementations.

This script is run by the UVManagedPyImplProvider in a subprocess within a UV-managed
virtual environment. It:

1. Receives a registration URL and token from the parent process
2. Creates a spawner implementation that can dynamically instantiate Python implementations
3. Registers the spawner with the parent process using the provided token
4. Waits for spawn requests and creates implementations on-demand
5. Waits for SIGINT and then shuts down gracefully

Usage:
    python -m behaviors.staging.providers.uv_py_provider_entrypoint <register_url> <token>
"""

import logging
import os
import signal
import sys
import traceback

import anyio
import anyio.abc
import logfire

from ..impl_plans.py_impl_plan import PyImplPlan
from ..impls import impl_remote, implserver
from ..object_model import core
from ..providers import in_process_py_provider
from .uv_py_provider import _UV_REGISTER_PROTOCOL, _UV_SPAWNER_PROTOCOL, DependencyPlanItem

logger = logging.getLogger(__name__)


class _SpawnerImpl(_UV_SPAWNER_PROTOCOL):
    """Spawner implementation that dynamically instantiates Python implementations."""

    interface_spec = core.INFER_INTERFACE_FROM_BASE_CLASS

    def __init__(
        self,
        task_group: anyio.abc.TaskGroup,
        shutdown_event: anyio.Event,
    ):
        self._provider = in_process_py_provider.InProcessPyProvider()
        self._task_group = task_group
        self._shutdown_event = shutdown_event

    async def spawn(
        self,
        plan: PyImplPlan,
        dependency_plan_items: list[DependencyPlanItem],
    ) -> core.FxtrCallTarget:
        """Spawn a new implementation for the given plan and dependencies."""

        logger.debug("Spawner spawning for plan: %s", id(plan))

        # Convert dependency plan-impl pairs to dict
        dep_routes = {}
        for dep_item in dependency_plan_items:
            dep_plan = dep_item["plan"]
            dep_impl = dep_item["impl"]
            dep_routes[dep_plan] = dep_impl

        # Use InProcessPyProvider to instantiate the implementation
        logger.debug("Spawner about to call provide_one_no_provenance for plan: %s", id(plan))
        impl = await self._provider.provide_one_no_provenance(plan, dep_routes)
        logger.debug("Spawner finished provide_one_no_provenance for plan: %s", id(plan))
        return impl


class UVSpawnerEntrypoint:
    """Main entrypoint class for UV spawner processes."""

    def __init__(self, register_url: str, token: str):
        self.register_url = register_url
        self.token = token
        self.shutdown_event = anyio.Event()

    async def run(self) -> None:
        """Main execution method."""

        try:
            async with anyio.create_task_group() as tg:
                # Set up signal handling for graceful shutdown
                tg.start_soon(self._handle_signals)
                tg.start_soon(self._main_logic)
                logger.info(f"PID {os.getpid()}: UV spawner entrypoint is running")
            logger.debug(f"PID {os.getpid()}: UV spawner entrypoint exiting")
        except Exception as e:
            logger.exception(f"PID {os.getpid()}: Error in UV spawner entrypoint")
            sys.exit(1)

    async def _handle_signals(self) -> None:
        """Handle SIGINT and SIGTERM signals for graceful shutdown."""
        with anyio.open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signal_receiver:
            async for signum in signal_receiver:
                logger.info(
                    f"PID {os.getpid()}: Spawner received signal {signum}, shutting down gracefully..."
                )
                self.shutdown_event.set()
                return  # Exit after handling the first signal
        logger.debug(f"PID {os.getpid()}: _handle_signals exiting")

    async def _main_logic(self) -> None:
        """Main spawner logic."""
        # Step 1: Set up the implementation server
        async with implserver.ImplServer().serving() as server:
            # Step 2: Create task group for spawner
            async with anyio.create_task_group() as spawner_tg:
                # Step 3: Create the spawner implementation
                spawner_impl = _SpawnerImpl(spawner_tg, self.shutdown_event)

                # Step 4: Serve the spawner implementation
                with server.serve_impl(spawner_impl) as spawner_url:
                    # Step 5: Register the spawner with the parent process
                    await self._register_spawner(spawner_url)

                    # Step 6: Wait for shutdown signal
                    logger.info(f"PID {os.getpid()}: Spawner ready and waiting for requests...")
                    await self.shutdown_event.wait()
                    logger.info(f"PID {os.getpid()}: Spawner shutting down...")

                logger.debug(f"PID {os.getpid()}: Spawner no longer being served.")
            logger.debug(f"PID {os.getpid()}: Spawner exited spawner_tg.")
        logger.debug(f"PID {os.getpid()}: Spawner exited _main_logic.")

    async def _register_spawner(self, spawner_url: str) -> None:
        """Register the spawner URL with the parent process using the provided token."""

        async with impl_remote.RemoteImpl.connect(
            interface_spec=core.spec_of(_UV_REGISTER_PROTOCOL),
            base_url=self.register_url,
        ) as remote_impl:
            await remote_impl.register(self.token, spawner_url)


async def main() -> None:
    """Main entrypoint function."""
    logfire.configure(
        distributed_tracing=True,
        inspect_arguments=False,
        send_to_logfire="if-token-present",
    )
    logfire.instrument_httpx()

    if len(sys.argv) != 3:
        # This is a usage error message, keep it as print to stderr since logging may not be configured
        print(
            "Usage: python -m fxtr.providers.uv_py_provider_entrypoint <register_url> <token>",
            file=sys.stderr,
        )
        sys.exit(1)

    register_url = sys.argv[1]
    token = sys.argv[2]
    entrypoint = UVSpawnerEntrypoint(register_url, token)
    await entrypoint.run()


if __name__ == "__main__":
    # anyio.run(main, backend_options={"debug": True})
    anyio.run(main)
