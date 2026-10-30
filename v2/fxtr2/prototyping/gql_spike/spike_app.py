"""Combined GraphQL + frontend server for the spike explorer."""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import subprocess
import time

from ariadne.asgi import GraphQL
from ariadne.asgi.handlers import GraphQLTransportWSHandler
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from fxtr2.prototyping.gql_spike.spike_server import (
    Foo,
    FooDict,
    FooSingle,
    FooStreamRef,
    add_root,
    add_stream,
    emit_stream_item,
    finalize_stream,
    registry_roots,
    registry_streams,
    remove_root,
    remove_stream,
    schema,
)

# ============================================================================
# Demo endpoint
# ============================================================================

# Track running demo task so we can prevent concurrent runs
_demo_task: asyncio.Task | None = None


async def _run_child_counter(stream_id: str, delay: float, child_index: int) -> None:
    """Run a child stream that counts up to 10 at the given speed."""
    for count in range(1, 11):
        await asyncio.sleep(delay)
        await emit_stream_item(
            stream_id,
            FooSingle(value=Foo(id=f"{stream_id}_item_{count}", data=f"Count: {count}")),
        )

    # Create final result root
    final_root = await add_root(
        f"{stream_id}_result",
        FooSingle(
            value=Foo(id=f"{stream_id}_final", data=f"Child {child_index} completed with count 10")
        ),
    )
    await finalize_stream(stream_id, final_root.id)


async def _run_demo() -> str:
    """Run the demo process.

    1. Clear old demo roots/streams from registry
    2. Create a key root for the parent stream
    3. Create parent stream with that key
    4. Spawn child streams that count up at varying speeds
    5. Each child finalizes to a new root
    6. Parent stream finalizes to a summary root

    Returns the parent stream ID.
    """
    ts = int(time.time() * 1000)

    # 1. Clear old demo items (anything starting with "demo_")
    for root_id in list(registry_roots):
        if root_id.startswith("demo_"):
            await remove_root(root_id)
    for stream_id in list(registry_streams):
        if stream_id.startswith("demo_"):
            await remove_stream(stream_id)

    # 2. Create key root for parent stream
    key_root = await add_root(
        f"demo_key_{ts}",
        FooSingle(value=Foo(id=f"key_{ts}", data="Demo process key")),
    )

    # 3. Create parent stream
    parent_stream = await add_stream(f"demo_parent_{ts}", key_root.id)

    await asyncio.sleep(5.0)

    # 4. Spawn child streams at different speeds
    child_tasks = []
    speeds = [t * 3 for t in [0.1, 0.2, 0.3, 0.5, 0.7]]  # seconds between counts

    for i, speed in enumerate(speeds):
        await asyncio.sleep(1.0)

        child_id = f"demo_child_{ts}_{i}"
        await add_stream(child_id)

        # Emit ref to child in parent stream
        await emit_stream_item(
            parent_stream.id,
            FooStreamRef(target_id=child_id),
        )

        # Start async task for this child
        task = asyncio.create_task(_run_child_counter(child_id, speed, i))
        child_tasks.append(task)

    # 5. Wait for all children to complete
    await asyncio.gather(*child_tasks)

    # 6. Finalize parent stream
    summary_root = await add_root(
        f"demo_summary_{ts}",
        FooDict(
            entries={
                "completed": FooSingle(value=Foo(id=f"done_{ts}", data="All children completed")),
                "child_count": FooSingle(value=Foo(id=f"count_{ts}", data=str(len(speeds)))),
            }
        ),
    )
    await finalize_stream(parent_stream.id, summary_root.id)

    return parent_stream.id


async def _demo_endpoint(request: Request) -> JSONResponse:
    """POST /api/demo/start

    Triggers the demo process that creates parent/child streams.
    """
    global _demo_task

    # Check if demo is already running
    if _demo_task is not None and not _demo_task.done():
        return JSONResponse(
            {"error": "Demo is already running", "ok": False},
            status_code=409,
        )

    # Start the demo in a background task so we can return immediately
    _demo_task = asyncio.create_task(_run_demo())

    # Wait a tiny bit to get the parent stream ID
    # (The parent stream is created synchronously at the start of _run_demo)
    await asyncio.sleep(0.01)

    return JSONResponse({"ok": True, "message": "Demo started"})


class SpikeExplorerApp:
    """Combined server for GraphQL API and TypeScript frontend."""

    def __init__(self, *, dev_mode: bool = False):
        self.dev_mode = dev_mode
        self.compiled_js: str | None = None

        # Find the TypeScript package directory
        self.ts_package_dir = pathlib.Path(__file__).parent / "ts"
        self.main_tsx_file = self.ts_package_dir / "main.tsx"
        self.html_file = self.ts_package_dir / "index.html"

        if not self.dev_mode:
            self._compile_typescript()

    def _compile_typescript(self) -> str:
        """Compile the TypeScript main file using esbuild."""
        if not self.main_tsx_file.exists():
            raise FileNotFoundError(f"Main TypeScript file not found: {self.main_tsx_file}")

        # Read the TypeScript source
        build_stdin = self.main_tsx_file.read_text()

        # Run esbuild to compile
        subprocess.run(["pnpm", "install"], check=True, cwd=self.ts_package_dir)
        build_result = subprocess.run(
            ["pnpm", "exec", "esbuild", "--bundle", "--loader=tsx", "--format=esm"],
            check=False,
            text=True,
            input=build_stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.ts_package_dir,
        )

        if build_result.returncode != 0:
            raise RuntimeError(f"esbuild compilation failed: {build_result.stderr}")

        self.compiled_js = build_result.stdout
        return build_result.stdout

    async def _serve_html(self, request):
        """Serve the index HTML page."""
        if not self.html_file.exists():
            return Response("HTML file not found", status_code=404, media_type="text/plain")

        html_content = self.html_file.read_text()
        return Response(html_content, media_type="text/html")

    async def _serve_compiled_js(self, request):
        """Serve the compiled JavaScript at /app/main.js."""
        if self.dev_mode or self.compiled_js is None:
            try:
                self._compile_typescript()
                if self.dev_mode:
                    print("Recompiled TypeScript in dev mode")
            except Exception as e:
                return Response(f"Compilation error: {e}", status_code=500, media_type="text/plain")

        return Response(self.compiled_js, media_type="application/javascript")

    def create_app(self) -> Starlette:
        """Create the Starlette application."""
        # Use GraphQLTransportWSHandler for the newer graphql-transport-ws protocol
        # (required by the graphql-ws client library)
        graphql_app = GraphQL(
            schema,
            debug=True,
            websocket_handler=GraphQLTransportWSHandler(),
        )

        routes = [
            Route("/", self._serve_html),
            Route("/app/main.js", self._serve_compiled_js),
            Mount("/graphql", app=graphql_app),
            # Demo endpoint
            Route("/api/demo/start", _demo_endpoint, methods=["POST"]),
        ]

        app = Starlette(routes=routes)

        # Add CORS middleware for development
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        return app


def create_app(dev_mode: bool = False) -> Starlette:
    """Factory function to create the app."""
    explorer = SpikeExplorerApp(dev_mode=dev_mode)
    return explorer.create_app()


# Default app for uvicorn (production mode)
app = create_app(dev_mode=False)


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="Run the spike explorer server")
    parser.add_argument(
        "--dev", action="store_true", help="Run in development mode with TypeScript recompilation"
    )
    parser.add_argument("--host", default="localhost", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind to")
    args = parser.parse_args()

    # Create app with appropriate mode
    application = create_app(dev_mode=args.dev)

    print(f"Starting spike explorer at http://{args.host}:{args.port}")
    if args.dev:
        print("Development mode: TypeScript will be recompiled on each request")

    uvicorn.run(application, host=args.host, port=args.port)
