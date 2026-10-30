"""Panel spike server - TypeScript + REST only."""

from __future__ import annotations

import argparse
import pathlib
import subprocess

from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route


class PanelSpikeApp:
    """Server for TypeScript frontend with REST API."""

    def __init__(self, *, dev_mode: bool = False):
        self.dev_mode = dev_mode
        self.compiled_js: str | None = None

        # Find the TypeScript package directory
        self.ts_package_dir = pathlib.Path(__file__).parent / "ts"
        self.main_tsx_file = self.ts_package_dir / "main.tsx"
        self.html_file = self.ts_package_dir / "index.html"
        self.popout_html_file = self.ts_package_dir / "popout.html"

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

    async def _serve_html(self, request: Request) -> Response:
        """Serve the index HTML page."""
        if not self.html_file.exists():
            return Response("HTML file not found", status_code=404, media_type="text/plain")

        html_content = self.html_file.read_text()
        return Response(html_content, media_type="text/html")

    async def _serve_compiled_js(self, request: Request) -> Response:
        """Serve the compiled JavaScript at /app/main.js."""
        if self.dev_mode or self.compiled_js is None:
            try:
                self._compile_typescript()
                if self.dev_mode:
                    print("Recompiled TypeScript in dev mode")
            except Exception as e:
                return Response(f"Compilation error: {e}", status_code=500, media_type="text/plain")

        return Response(self.compiled_js, media_type="application/javascript")

    async def _serve_popout_html(self, request: Request) -> Response:
        """Serve the popout HTML page for dockview popout windows."""
        if not self.popout_html_file.exists():
            return Response("Popout HTML file not found", status_code=404, media_type="text/plain")

        html_content = self.popout_html_file.read_text()
        return Response(html_content, media_type="text/html")

    async def _health_check(self, request: Request) -> JSONResponse:
        """GET /api/health - simple health check endpoint."""
        return JSONResponse({"ok": True, "status": "healthy"})

    def create_app(self) -> Starlette:
        """Create the Starlette application."""
        routes = [
            Route("/", self._serve_html),
            Route("/popout.html", self._serve_popout_html),
            Route("/app/main.js", self._serve_compiled_js),
            Route("/api/health", self._health_check, methods=["GET"]),
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
    spike = PanelSpikeApp(dev_mode=dev_mode)
    return spike.create_app()


# Default app for uvicorn (production mode)
app = create_app(dev_mode=False)


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="Run the panel spike server")
    parser.add_argument(
        "--dev", action="store_true", help="Run in development mode with TypeScript recompilation"
    )
    parser.add_argument("--host", default="localhost", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8001, help="Port to bind to")
    args = parser.parse_args()

    # Create app with appropriate mode
    application = create_app(dev_mode=args.dev)

    print(f"Starting panel spike at http://{args.host}:{args.port}")
    if args.dev:
        print("Development mode: TypeScript will be recompiled on each request")

    uvicorn.run(application, host=args.host, port=args.port)
