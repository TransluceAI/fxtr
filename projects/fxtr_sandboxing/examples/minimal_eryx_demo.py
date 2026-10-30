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

import concurrent.futures
import json
import os
import queue
import tempfile
import textwrap

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

    factory = eryx.SandboxFactory()

    # --- 3. Write task source files to a temp directory ---
    # In production, these come from the TaskBundle entity. Here we
    # write them to a temp dir and volume-mount them into the sandbox.
    task_source = textwrap.dedent(
        """\
        import json

        async def run(ctx):
            \"\"\"Entry point: echoes messages back with six version info.\"\"\"
            while True:
                raw = await ctx.receive()
                msg = json.loads(raw)
                if msg.get("type") == "shutdown":
                    break
                msg["from"] = "sandbox"
                await ctx.send(json.dumps(msg))
    """
    )

    with tempfile.TemporaryDirectory() as task_dir:
        with open(os.path.join(task_dir, "hello.py"), "w") as f:
            f.write(task_source)

        # --- 4. Create sandbox with wheel + volume mount + callbacks ---
        # volumes format: List[Tuple[host_path, guest_path, read_only]]
        sandbox = factory.create_sandbox(
            callbacks=registry,
            volumes=[(task_dir, "/task", True)],
        )

        # --- 5. The runner script ---
        # This is what we'd inject at execution time. It:
        # - Sets up sys.path so the task module is importable
        # - Defines TaskContext wrapping the raw callbacks
        # - Imports the entry point and calls it
        runner_code = textwrap.dedent(
            """\
            import sys
            sys.path.insert(0, "/task")

            class TaskContext:
                async def receive(self):
                    return await _receive_from_host()
                async def send(self, data):
                    await _send_to_host(data=data)

            import hello
            await hello.run(TaskContext())
        """
        )

        # --- 6. Run the sandbox in a thread, communicate from main thread ---
        # sandbox.execute() is blocking, so we run it in a thread pool.
        def run_sandbox():
            return sandbox.execute(runner_code)

        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(run_sandbox)

            # Send a message and get a response
            host_to_sandbox.put(json.dumps({"hello": "world"}))
            resp = json.loads(sandbox_to_host.get(timeout=10))
            print(f"Response 1: {resp}")
            assert resp["hello"] == "world"
            assert resp["from"] == "sandbox"

            # Send another
            host_to_sandbox.put(json.dumps({"count": 42}))
            resp = json.loads(sandbox_to_host.get(timeout=10))
            print(f"Response 2: {resp}")
            assert resp["count"] == 42

            # Shut down
            host_to_sandbox.put(json.dumps({"type": "shutdown"}))
            result = future.result(timeout=10)
            print(f"Sandbox finished. stderr: {result.stderr!r}")

    print("\nAll good!")


if __name__ == "__main__":
    main()
