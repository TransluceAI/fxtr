"""The debug viewer's Starlette app: the viewer itself, its API, and the client.

``create_app()`` builds a Starlette application holding one
:class:`~fxtr.runner.postgres.client.LocalProjectClient`, entered for the app's lifetime and used
only to read. Everything the viewer asks for is under ``/api``: Ariadne's GraphQL handler, over
HTTP only (:func:`http_only`), at ``/api/graphql``, raw entity bytes at
``GET /api/entity/{cid}`` (and many at once at ``POST /api/entities``, see
:func:`entities_request_ids`), the project's view bundle at ``GET /api/views/{file}``
(:func:`views_response`), where a function is implemented at ``GET /api/source/{name}`` (as a
job's code version records it, :func:`recorded_source_response`, else as the viewer's registry has
it, :func:`source_response`), a file of the project's repository at a commit or in the working
tree at ``GET /api/file`` (:func:`file_response`), what the server serves at ``GET /api/info``
(the project's identity, which :mod:`fxtr.project.viewer` finds the viewer by), the project's
EXPERIMENT.md at ``GET /api/experiment_readme`` (:func:`experiment_readme_response`), the view
bundle's state at ``GET /api/views/status``, and the viewer's events on the WebSocket
``/api/events`` (:mod:`.events`): the view bundle's changes, and each job a launcher announces at
``POST /api/launches``. Every other path is the viewer (:class:`Viewer`), so the viewer and its
API share one origin and the viewer's URLs are origin-relative. That is the built viewer
(:class:`ViewerFiles`: the copy an installed ``fxtr`` carries, else ``frontend/dist`` of the
checkout); or, given a dev server configuration in a checkout (:func:`checkout_frontend`), a
redirect to the viewer's Vite dev server (:mod:`.dev_server`), which the app runs for its lifetime
and which proxies ``/api`` back here.

The server never writes: it uses the client's inspection surface (``client.jobs``) and its entity
store, with the ordinary role; a launch announced to it goes only to the open pages. Without a
client of your own it serves the project the working directory belongs to
(:meth:`LocalProjectClient.from_project`; ``FXTR_DATABASE_URL`` overrides the project's local
configuration as usual).
"""

from __future__ import annotations

import ast
import contextlib
import importlib.metadata
import inspect
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path, PurePosixPath

import anyio.to_thread
from ariadne import make_executable_schema
from ariadne.asgi import GraphQL
from graphql import GraphQLSchema
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket, WebSocketClose

from fxtr.core import codec
from fxtr.core.entity_data import EntityID, cid_from_str
from fxtr.experiment.registry import FunctionRegistry, RegistryError
from fxtr.project.code_version import CodeVersion, RepoSource
from fxtr.project.config import Project
from fxtr.project.viewer import project_identity
from fxtr.project.views import VIEW_BUNDLE_MANIFEST_FILENAME
from fxtr.runner.postgres.client import LocalProjectClient
from fxtr.runner.postgres.debug_web import resolvers
from fxtr.runner.postgres.debug_web.dev_server import DevServerConfig, run_dev_server
from fxtr.runner.postgres.debug_web.events import (
    EventHub,
    follow_views,
    launch_event,
    launch_request,
    same_origin,
    serve_events,
)
from fxtr.runner.postgres.debug_web.output import OutputStore
from fxtr.runner.postgres.debug_web.viewer_build import refresh_viewer
from fxtr.runner.postgres.debug_web.views_watch import ViewsWatcher

_NO_CACHE = {"Cache-Control": "no-cache"}
"""Every view bundle and viewer response is revalidated, so a page reload picks up a rebuild."""

VIEWS_VERSION_HEADER = "X-Fxtr-Views-Version"
"""On a bundle's manifest: the bundle's version (:func:`~fxtr.project.views.bundle_version`), what
the frontend compares with the versions ``/api/events`` announces."""

PACKAGED_FRONTEND_DIST = Path(__file__).resolve().parent / "_frontend"
"""The built viewer an installed ``fxtr`` sdist or wheel carries (see ``fxtr/hatch_build.py``)."""

CHECKOUT_FRONTEND_DIST = Path(__file__).resolve().parents[6] / "frontend" / "dist"
"""Where the checkout this module lives in builds the viewer (``pnpm --filter fxtr-ide build``)."""


def default_frontend_dist() -> Path:
    """The packaged viewer when this ``fxtr`` is an installed build, else the checkout's."""
    return PACKAGED_FRONTEND_DIST if PACKAGED_FRONTEND_DIST.is_dir() else CHECKOUT_FRONTEND_DIST


def checkout_frontend() -> Path | None:
    """The checkout's ``frontend/`` when this ``fxtr`` runs from a checkout (no packaged viewer,
    the frontend's sources beside it), else ``None``: where the viewer's dev server runs."""
    if PACKAGED_FRONTEND_DIST.is_dir():
        return None
    frontend = CHECKOUT_FRONTEND_DIST.parent
    return frontend if (frontend / "package.json").is_file() else None


class ViewerFiles(StaticFiles):
    """The built viewer (``index.html`` at ``/``), every file revalidated on each load (so a
    browser never mixes a rebuilt viewer with cached files from the old one).

    The directory may be missing, and may appear while the server runs: until it has an
    ``index.html``, every path answers 404 with how to build the viewer or run its dev server.
    """

    def __init__(self, directory: Path) -> None:
        super().__init__(directory=directory, html=True, check_dir=False)
        self.dist = directory

    async def check_config(self) -> None:
        pass

    async def get_response(self, path: str, scope: Scope) -> Response:
        if not (self.dist / "index.html").is_file():
            return PlainTextResponse(
                f"The viewer is not built ({self.dist} has no index.html). Build it with"
                " `pnpm --filter fxtr-ide build` in the fxtr checkout, or run `pnpm install`"
                " there so that `fxtr view` runs the viewer's dev server.",
                404,
                headers=_NO_CACHE,
            )
        response = await super().get_response(path, scope)
        response.headers.update(_NO_CACHE)
        return response


class Viewer:
    """Every path outside ``/api``: the same path on the viewer's dev server while
    ``dev_port`` is set (a temporary redirect, to the host the request named; the ``#job=``
    fragment carries over, as browsers keep it across redirects); a 503 saying why while
    ``refusal`` is set (a stale built viewer that could not be rebuilt); else
    :class:`ViewerFiles`."""

    def __init__(self, files: ViewerFiles) -> None:
        self.files = files
        self.dev_port: int | None = None
        self.refusal: str | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.refusal is not None and scope["type"] == "http":
            await PlainTextResponse(self.refusal, 503, headers=_NO_CACHE)(scope, receive, send)
            return
        if self.dev_port is not None and scope["type"] == "http":
            url = Request(scope).url.replace(port=self.dev_port)
            await RedirectResponse(str(url), headers=_NO_CACHE)(scope, receive, send)
            return
        await self.files(scope, receive, send)


def _load_sdl() -> str:
    import pathlib

    return (pathlib.Path(__file__).parent / "schema.graphql").read_text()


def views_response(views: ViewsWatcher | None, name: str, version: str | None = None) -> Response:
    """Serve bytes from the current snapshot, never reopen a mutable build directory.

    An unversioned request reads the current snapshot. A requested version that has expired
    returns 410 so the browser can restart from the latest manifest. No configured views is a
    404; an unavailable initial bundle is a 409 with the reason.
    """
    if views is None:
        return Response("the project configures no view bundle", 404, headers=_NO_CACHE)
    bundle = views.bundle(version)
    if bundle is None:
        if version is not None:
            return Response("the requested view bundle has expired", 410, headers=_NO_CACHE)
        return Response(views.state.error or "the view bundle is not ready", 409, headers=_NO_CACHE)
    manifest = bundle.manifest
    headers = dict(_NO_CACHE)
    if name == VIEW_BUNDLE_MANIFEST_FILENAME:
        headers[VIEWS_VERSION_HEADER] = bundle.version
        media_type = "application/json"
    elif name == manifest.module:
        media_type = "text/javascript"
    elif name == manifest.stylesheet:
        media_type = "text/css"
    else:
        return Response(status_code=404, headers=_NO_CACHE)
    return Response(bundle.files[name], media_type=media_type, headers=headers)


def source_response(registry: FunctionRegistry, name: str) -> Response:
    """Where the step or workflow registered as ``name`` is implemented, and its docstring, as the
    working tree has it (not necessarily the code a job ran): what the viewer says of a function
    no code version records in the repository (see :func:`recorded_source_response`).

    The JSON has ``file`` (relative to ``repoRoot``, the repository enclosing the file, or the
    file's directory outside one), ``repoRoot``, ``symbol``, ``startLine`` and ``endLine`` (the
    decorators included), ``docstring``, and ``commit`` (``null``: the working tree's). 404 when
    nothing, or more than one function, is registered under the name: the project's
    ``[tool.fxtr] modules`` are what the viewer imports.
    """
    try:
        fn = registry.function(name)
    except RegistryError as exc:
        return Response(str(exc), 404, media_type="text/plain")
    body = inspect.unwrap(fn.body)
    source_file = inspect.getsourcefile(body)
    if source_file is None:
        return Response(f"{name!r} has no source file", 404, media_type="text/plain")
    file = Path(source_file).resolve()
    lines, start = inspect.getsourcelines(body)
    root = enclosing_repository(file.parent) or file.parent
    return JSONResponse(
        {
            "file": file.relative_to(root).as_posix(),
            "repoRoot": str(root),
            "symbol": body.__qualname__,
            "startLine": start,
            "endLine": start + len(lines) - 1,
            "docstring": inspect.getdoc(body) or "",
            "commit": None,
        }
    )


def recorded_source_response(
    source: RepoSource, commit: str, repo_root: Path, committed: bytes | None
) -> Response:
    """Where a code version records a function inside its repository, and the function's
    docstring as the pinned commit has it: what the viewer says of a node's function, so its code
    link and docs follow the code the job ran rather than whatever the viewer's process imported.

    The JSON is :func:`source_response`'s, with ``repoRoot`` this machine's checkout of the
    repository (the code version records no machine's path) and ``commit`` the pinned commit. The
    lines are the commit's, so the working tree may have moved them since.

    Args:
        source: The function, as the code version records it.
        commit: The code version's commit.
        repo_root: The repository enclosing the viewed project, here.
        committed: ``source.file`` as ``commit`` has it; ``None`` when the repository here does
            not have it, which leaves the docstring empty.
    """
    return JSONResponse(
        {
            "file": source.file,
            "repoRoot": str(repo_root),
            "symbol": source.symbol,
            "startLine": source.start_line,
            "endLine": source.end_line,
            "docstring": "" if committed is None else docstring_at(committed, source.start_line),
            "commit": commit,
        }
    )


def docstring_at(module_source: bytes, start_line: int) -> str:
    """The docstring of the function or class in ``module_source`` whose definition, decorators
    included, starts at ``start_line`` (as :func:`inspect.getdoc` cleans it), or ``""`` when it has
    none or nothing parses there."""
    try:
        tree = ast.parse(module_source)
    except (SyntaxError, ValueError):
        return ""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
            if first == start_line:
                return ast.get_docstring(node) or ""
    return ""


MAX_FILE_BYTES = 2_000_000
"""The largest source file ``GET /api/file`` serves; the viewer shows code, not data."""

_COMMIT = re.compile(r"[0-9a-f]{4,64}")


def file_request_path(repo_root: Path, path: str) -> str | None:
    """``path`` as a POSIX path relative to ``repo_root`` when it names something inside the
    repository (the working tree's file, resolved, must stay inside it), else ``None``."""
    relative = PurePosixPath(path)
    if not path or relative.is_absolute() or ".." in relative.parts:
        return None
    resolved = (repo_root / relative).resolve()
    return relative.as_posix() if resolved.is_relative_to(repo_root.resolve()) else None


def file_response(path: str, commit: str | None, contents: bytes | None) -> Response:
    """A source file of the project's repository, for the viewer's code pane: JSON ``{path,
    commit, text}``, ``commit`` being the commit it was read at or ``null`` for the working tree.
    404 when there is no such file there, 413 past :data:`MAX_FILE_BYTES`."""
    if contents is None:
        at = "the working tree" if commit is None else f"commit {commit}"
        return Response(f"{path} is not in {at}", 404, media_type="text/plain")
    if len(contents) > MAX_FILE_BYTES:
        return Response(f"{path} is too large to show", 413, media_type="text/plain")
    return JSONResponse(
        {"path": path, "commit": commit, "text": contents.decode("utf-8", errors="replace")}
    )


def enclosing_repository(path: Path) -> Path | None:
    """The jj or git repository ``path`` (a directory) lies in, or ``None`` outside one."""
    return next(
        (d for d in (path, *path.parents) if (d / ".jj").is_dir() or (d / ".git").exists()), None
    )


def experiment_readme_response(project: Project) -> Response:
    """The project root's ``EXPERIMENT.md``, as JSON ``{path, markdown}``; 404 without one."""
    path = project.root / "EXPERIMENT.md"
    if not path.is_file():
        return Response("the project has no EXPERIMENT.md", 404, media_type="text/plain")
    return JSONResponse({"path": str(path), "markdown": path.read_text()})


MAX_ENTITIES_PER_REQUEST = 200
"""The most entities one ``POST /api/entities`` may ask for."""


def entities_request_ids(body: bytes) -> list[EntityID]:
    """The ids a ``POST /api/entities`` body asks for: JSON ``{"cids": [...]}``, at most
    :data:`MAX_ENTITIES_PER_REQUEST` cid strings, repeats allowed.

    The response is a DAG-CBOR list in the same order, each item the entity's stored DRISL bytes
    as a byte string (what ``GET /api/entity/{cid}`` serves) or null when it is not stored.

    Raises:
        ValueError: If the body is not that shape or names a malformed cid.
    """
    try:
        request = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"the body is not JSON: {exc}") from exc
    cids = request.get("cids") if isinstance(request, dict) else None
    if not isinstance(cids, list) or not all(isinstance(cid, str) for cid in cids):
        raise ValueError('the body must be {"cids": [<cid string>, ...]}')
    if len(cids) > MAX_ENTITIES_PER_REQUEST:
        raise ValueError(
            f"{len(cids)} cids asked for; at most {MAX_ENTITIES_PER_REQUEST} per request"
        )
    ids = []
    for cid in cids:
        try:
            ids.append(EntityID(cid=cid_from_str(cid)))
        except ValueError as exc:
            raise ValueError(f"malformed cid {cid!r}: {exc}") from exc
    return ids


def http_only(app: ASGIApp) -> ASGIApp:
    """``app`` for HTTP requests only: a WebSocket is closed before it opens.

    Ariadne's GraphQL app also serves GraphQL over a WebSocket, queries included, unless it is kept
    from it. The viewer reads GraphQL over HTTP, and browsers let a page of any site open a
    WebSocket to any address, so none is opened."""

    async def serve(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await WebSocketClose(code=1008)(scope, receive, send)
            return
        await app(scope, receive, send)

    return serve


def build_schema() -> GraphQLSchema:
    """The executable schema (the SDL bound to the resolvers)."""
    return make_executable_schema(_load_sdl(), *resolvers.BINDABLES)


def create_app(
    client: LocalProjectClient | None = None,
    *,
    frontend_dist: Path | None = None,
    dev_server: DevServerConfig | None = None,
) -> Starlette:
    """A Starlette app serving the debug viewer and its API.

    Args:
        client: The client to serve from, not yet entered: the app enters it for its lifetime and
            leaves it on shutdown. The working directory's project, with Ctrl-C left to the
            server, by default.
        frontend_dist: The built viewer to serve outside ``/api`` (see :class:`ViewerFiles`);
            :func:`default_frontend_dist` by default.
        dev_server: Run the viewer's dev server for the app's lifetime, and redirect the viewer's
            paths to it, when this ``fxtr`` runs from a checkout (:func:`checkout_frontend`).
            The built viewer is served when it is ``None``, outside a checkout, or when the dev
            server fails to start.

    The project's view bundle is rebuilt at startup when stale, then whenever its sources
    change. Build failures keep the last valid bundle, or leave the viewer on its defaults
    until a build succeeds. A checkout's built viewer is also rebuilt when stale before serving
    it (unless ``frontend_dist`` is given); if that fails, only the API is served.
    """
    client = LocalProjectClient.from_project(handle_sigint=False) if client is None else client
    schema = build_schema()
    outputs = OutputStore(client)

    graphql_app = GraphQL(
        schema,
        context_value=lambda request, *_args, **_kwargs: {
            "client": client,
            "outputs": outputs,
            "request": request,
        },
    )

    async def entity_bytes(request: Request) -> Response:
        """The stored DRISL (DAG-CBOR) bytes of one entity, for the client to decode itself."""
        try:
            entity_id = EntityID(cid=cid_from_str(request.path_params["cid"]))
        except ValueError:
            return Response(status_code=400)
        result = await client.entities.load_raw_bytes(entity_id)
        if result is None:
            return Response(status_code=404)
        return Response(content=result[0], media_type="application/cbor")

    async def entities_bytes(request: Request) -> Response:
        """Many entities' stored bytes in one response; see :func:`entities_request_ids`."""
        try:
            entity_ids = entities_request_ids(await request.body())
        except ValueError as exc:
            return Response(str(exc), 400, media_type="text/plain")
        results = await client.entities.load_raw_bytes_many(entity_ids)
        body = codec.encode([None if result is None else result[0] for result in results])
        return Response(content=body, media_type="application/cbor")

    async def views_file(request: Request) -> Response:
        name = request.path_params["name"]
        version = request.query_params.get("v")
        return views_response(views, name, version)

    async def source(request: Request) -> Response:
        """Where a function is, from the code version named by ``?codeVersion=`` (a CID) when it
        records the function in the repository, else from the registry the viewer imported."""
        name = request.path_params["name"]
        recorded = request.query_params.get("codeVersion")
        repo_root = enclosing_repository(client.project.root.resolve())
        if recorded is not None and repo_root is not None:
            try:
                entity_id = EntityID(cid=cid_from_str(recorded))
            except ValueError:
                return Response("codeVersion is not a CID", 400, media_type="text/plain")
            code_version = await client.entities.load(entity_id, as_type=CodeVersion)
            found = code_version.functions.get(name)
            if isinstance(found, RepoSource):
                committed = await client.environment.committed_file(
                    client.project, code_version.commit, found.file
                )
                return recorded_source_response(found, code_version.commit, repo_root, committed)
        return source_response(client.registry, name)

    async def file(request: Request) -> Response:
        """A file of the project's repository: at ``?commit=`` when given, else the working
        tree's (see :func:`file_response`)."""
        repo_root = enclosing_repository(client.project.root.resolve())
        requested = request.query_params.get("path", "")
        path = None if repo_root is None else file_request_path(repo_root, requested)
        if repo_root is None or path is None:
            return Response(f"{requested!r} is not in the repository", 404, media_type="text/plain")
        commit = request.query_params.get("commit")
        if commit is not None and not _COMMIT.fullmatch(commit):
            return Response("commit is not a commit hash", 400, media_type="text/plain")
        if commit is not None:
            contents = await client.environment.committed_file(client.project, commit, path)
        else:
            target = repo_root / path
            contents = target.read_bytes() if target.is_file() else None
        return file_response(path, commit, contents)

    async def experiment_readme(_: Request) -> Response:
        return experiment_readme_response(client.project)

    async def info(_: Request) -> Response:
        return JSONResponse(
            {**project_identity(client.project), "fxtrVersion": importlib.metadata.version("fxtr")},
            headers=_NO_CACHE,
        )

    views = ViewsWatcher(client.project.views) if client.project.views is not None else None

    async def views_status(_: Request) -> Response:
        if views is None:
            return Response("the project configures no view bundle", 404, headers=_NO_CACHE)
        return JSONResponse(views.state.to_json(), headers=_NO_CACHE)

    hub = EventHub()

    async def events(websocket: WebSocket) -> None:
        await serve_events(websocket, hub)

    async def launches(request: Request) -> Response:
        """Tell the open pages that a job was launched (see :mod:`.events`): JSON
        ``{"jobId": ...}`` from no page or this viewer's own, answering how many pages heard it as
        ``{"listeners": n}``. Being JSON, another site's page cannot send it without a CORS
        preflight, which this server never grants."""
        if not same_origin(request.headers):
            return Response(
                "only the viewer's own pages may announce a launch", 403, media_type="text/plain"
            )
        content_type = request.headers.get("content-type", "").partition(";")[0]
        if content_type.strip().lower() != "application/json":
            return Response("a launch is announced as JSON", 415, media_type="text/plain")
        try:
            job_id = launch_request(await request.body())
        except ValueError as exc:
            return Response(str(exc), 400, media_type="text/plain")
        event = await launch_event(client, job_id)
        if event is None:
            return Response(f"the project has no job {job_id}", 404, media_type="text/plain")
        return JSONResponse({"listeners": hub.publish(event)}, headers=_NO_CACHE)

    viewer = Viewer(ViewerFiles(frontend_dist or default_frontend_dist()))

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with client, contextlib.AsyncExitStack() as stack:
            if views is not None:
                watching = await stack.enter_async_context(anyio.create_task_group())
                await watching.start(views.run)
                watching.start_soon(follow_views, views, hub)
                stack.callback(watching.cancel_scope.cancel)
            frontend = checkout_frontend()
            if dev_server is not None and frontend is not None:
                viewer.dev_port = await stack.enter_async_context(
                    run_dev_server(frontend, dev_server)
                )
            if viewer.dev_port is None and frontend is not None and frontend_dist is None:
                if dev_server is not None:
                    print(
                        "the viewer's dev server is not running: serving the built viewer in"
                        f" {frontend / 'dist'}",
                        flush=True,
                    )
                viewer.refusal = await refresh_viewer(frontend)
            try:
                yield
            finally:
                viewer.dev_port = None
                outputs.close()

    # GraphQL is mounted at /api/graphql, so clients use the mount's canonical path /api/graphql/
    # (with the trailing slash); the bare path redirects to it. The viewer's files come last, so
    # they never shadow the API.
    api = [
        Route("/entity/{cid}", entity_bytes),
        Route("/entities", entities_bytes, methods=["POST"]),
        Route("/views/status", views_status),
        Route("/views/{name}", views_file),
        WebSocketRoute("/events", events),
        Route("/launches", launches, methods=["POST"]),
        Route("/source/{name:path}", source),
        Route("/file", file),
        Route("/experiment_readme", experiment_readme),
        Route("/info", info),
        Mount("/graphql", http_only(graphql_app)),
    ]
    return Starlette(
        routes=[Mount("/api", routes=api), Mount("/", viewer)],
        lifespan=lifespan,
    )
