"""Debug visualizer server for entity graph object model using locators."""

import logging
import pathlib
import subprocess
from typing import Callable, Dict

import cbrrr
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from fxtr.api import api_lib, fxtrconfig
from fxtr.object_model import dag_util, locator

logger = logging.getLogger(__name__)


class FxtrLocatorDebugServer:
    """Debug server for entity graph visualization using a locator."""

    def __init__(
        self,
        data_locator: locator.Locator,
        named_urls: Dict[str, str],
        fxtr_config: fxtrconfig.FxtrConfig,
        *,
        dev_mode: bool = False,
        index_callback: Callable[[], str] | None = None,
    ):
        """Initialize the debug server.

        Args:
            data_locator: Locator to load entity data from
            named_urls: Dictionary mapping display names to actual URLs (e.g. {"demo_collection": "/view/collection/bafyrei..."})
            fxtr_config: The fxtr configuration for accessing run data
            dev_mode: Whether to run in development mode with TypeScript recompilation
            index_callback: Optional callback function that returns a URL for temporary redirect from index
        """
        self.data_locator = data_locator
        self.named_urls = named_urls
        self.fxtr_config = fxtr_config
        self.dev_mode = dev_mode
        self.index_callback = index_callback
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
            cwd=self.ts_package_dir,
        )

        if build_result.returncode != 0:
            raise RuntimeError(f"esbuild compilation failed: {build_result.stderr}")

        return build_result.stdout

    async def _serve_html(self, request):
        """Serve the static HTML page for any /view/ request."""
        if not self.html_file.exists():
            return PlainTextResponse("HTML file not found", status_code=404)

        html_content = self.html_file.read_text()
        return Response(html_content, media_type="text/html")

    async def _serve_compiled_js(self, request):
        """Serve the compiled JavaScript at /app/main.js."""
        if self.dev_mode or self.compiled_js is None:
            try:
                self.compiled_js = self._compile_typescript()
                if self.dev_mode:
                    logger.debug("Recompiled TypeScript in dev mode")
            except Exception as e:
                return PlainTextResponse(f"Compilation error: {e}", status_code=500)

        return Response(self.compiled_js, media_type="application/javascript")

    async def _serve_block(self, request):
        """Serve a JSON blob for a specific CID at /block/{cid}."""
        cid_str = request.path_params.get("cid")
        if not cid_str:
            return JSONResponse({"error": "CID parameter required"}, status_code=400)

        try:
            # Parse the CID string
            cid = cbrrr.CID.decode(cid_str)
        except Exception as e:
            return JSONResponse({"error": f"Invalid CID format: {e}"}, status_code=400)

        # Check if the locator has this entity
        if self.data_locator.has_entity(cid):
            try:
                # Load the raw entity data from the locator
                entity_data, new_locator = self.data_locator.retrieve(cid)
                if new_locator != self.data_locator:
                    logger.warning(
                        f"Cannot serve entities from new locator {new_locator}, received"
                        f" when retrieving entity {cid}"
                    )

                # Convert DAG-CBOR to escaped JSON format for the client
                escaped_json = dag_util.dag_cbor_to_escaped_atjson(entity_data)

                # Set Cache-Control header for 1 hour (3600 seconds)
                # It is always fine to cache this, because values are immutable and uniquely identified
                # by their CID.
                return JSONResponse(
                    escaped_json,
                    headers={"Cache-Control": "public, max-age=3600"},
                )
            except Exception as e:
                return JSONResponse({"error": f"Error loading entity: {e}"}, status_code=500)
        else:
            return JSONResponse({"error": f"CID not found: {cid_str}"}, status_code=404)

    async def _serve_task_runs(self, request):
        """Serve run data for a specific task at /task/{task_cid}/runs."""
        task_cid_str = request.path_params.get("task_cid")
        if not task_cid_str:
            return JSONResponse({"error": "task_cid parameter required"}, status_code=400)

        try:
            # Parse the CID string
            task_cid = cbrrr.CID.decode(task_cid_str)
        except Exception as e:
            return JSONResponse({"error": f"Invalid CID format: {e}"}, status_code=400)

        try:
            # Get runs for this task using the API
            runs = api_lib.runs_for_task(self.fxtr_config, task_cid)

            # Convert to JSON-serializable format
            runs_data = [
                {
                    "run_name": run.run_name,
                    "state": run.state,
                    "status_message": run.status_message,
                    "run_cid": run.run_cid.encode(base="base32") if run.run_cid else None,
                }
                for run in runs
            ]

            return JSONResponse({"task_cid": task_cid_str, "runs": runs_data})

        except Exception as e:
            return JSONResponse({"error": f"Error loading runs for task: {e}"}, status_code=500)

    async def _serve_index(self, request):
        """Serve an index page with links to named URLs."""
        # If index_callback is provided, use it for temporary redirect
        if self.index_callback is not None:
            try:
                redirect_url = self.index_callback()
                return Response(status_code=302, headers={"Location": redirect_url})
            except Exception as e:
                # If callback fails, fall back to normal index behavior
                logger.warning(f"index_callback failed: {e}")

        if not self.named_urls:
            return PlainTextResponse("No collections available", status_code=404)

        # If there's only one URL, redirect to it directly
        if len(self.named_urls) == 1:
            url = next(iter(self.named_urls.values()))
            return Response(status_code=302, headers={"Location": url})

        # Generate an HTML index page with links to all collections
        html_content = self._generate_index_html()
        return HTMLResponse(html_content)

    def _generate_index_html(self) -> str:
        """Generate HTML index page with links to all named collections."""
        links_html = ""
        for name, url in self.named_urls.items():
            # Extract CID from URL for display (assumes URL format like "/view/collection/{cid}")
            cid_display = url.split("/")[-1] if "/" in url else url
            links_html += f'<li><a href="{url}">{name}</a> <code>({cid_display})</code></li>\n'

        return f"""<!DOCTYPE html>
<html>
<head>
    <title>Fxtr Debug Visualizer</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
               max-width: 800px; margin: 2rem auto; padding: 0 1rem; }}
        h1 {{ color: #333; }}
        ul {{ line-height: 1.6; }}
        li {{ margin: 0.5rem 0; }}
        a {{ color: #0066cc; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
        code {{ background: #f5f5f5; padding: 0.2rem 0.4rem; border-radius: 3px;
                font-size: 0.9em; color: #666; }}
    </style>
</head>
<body>
    <h1>Fxtr Debug Visualizer</h1>
    <p>Available collections:</p>
    <ul>
{links_html}    </ul>
</body>
</html>"""

    def _create_app(self) -> Starlette:
        """Create the Starlette application."""
        routes = [
            Route("/", self._serve_index),
            Route("/view/{path:path}", self._serve_html),
            Route("/app/main.js", self._serve_compiled_js),
            Route("/block/{cid}", self._serve_block),
            Route("/task/{task_cid}/runs", self._serve_task_runs),
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

    async def serve(self, host: str = "localhost", port: int = 8000):
        """Start the debug server."""
        import uvicorn

        app = self._create_app()
        config = uvicorn.Config(app, host=host, port=port, log_level="info")
        server = uvicorn.Server(config)

        logger.info(f"Starting debug visualizer server at http://{host}:{port}")
        await server.serve()
