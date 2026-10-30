"""
Step 0 demo: validates that eryx works for our use case.

Demonstrates the full pattern:
- SandboxFactory with a pure-Python wheel (six)
- Volume mount for task source files
- Bidirectional communication via callbacks
- Top-level await in sandbox code

Run from the fxtr_sandboxing directory:
    uv run python examples/step0_eryx_demo.py
"""

import os
import pathlib
import queue
import shutil
import sys
import uuid

import eryx


def main():
    # --- 1. Set up bidirectional communication channels ---
    # These are thread-safe queues since sandbox.execute() is blocking
    # and runs in a thread pool.
    host_to_sandbox: queue.Queue[str] = queue.Queue()
    sandbox_to_host: queue.Queue[str] = queue.Queue()

    registry = eryx.CallbackRegistry()

    @registry.callback(description="Receive a message from the host")
    def _receive_from_host() -> str:
        return host_to_sandbox.get()

    @registry.callback(description="Send a message to the host")
    def _send_to_host(data: str) -> None:
        sandbox_to_host.put(data)
        return None

    if os.environ.get("TMPDIR"):
        tmp_dir_path = pathlib.Path(os.environ["TMPDIR"]) / "eryx-embedded"
        if tmp_dir_path.is_dir():
            clear_path = pathlib.Path(os.environ["TMPDIR"]) / f"eryx-embedded-old-{uuid.uuid4()}"
            tmp_dir_path.rename(clear_path)
            shutil.rmtree(clear_path)
            print(f"Cleared old temporary directory: {clear_path}")

    session = eryx.Session(
        on_stdout=lambda chunk: sys.stdout.write(f"[eryx stdout] {chunk}"),
        on_stderr=lambda chunk: sys.stderr.write(f"[eryx stderr] {chunk}"),
    )
    session.execute("print('Hello, world 1!')")

    sbx_factory = eryx.SandboxFactory()
    sbx = sbx_factory.create_sandbox(
        on_stdout=lambda chunk: sys.stdout.write(f"[eryx stdout] {chunk}"),
        on_stderr=lambda chunk: sys.stderr.write(f"[eryx stderr] {chunk}"),
    )
    sbx.execute("print('Hello, world 2!')")

    print("\nAll good!")


if __name__ == "__main__":
    main()
