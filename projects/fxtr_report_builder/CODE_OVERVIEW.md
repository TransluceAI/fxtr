# fxtr_report_builder

Python entity types + an MDX-to-FxtrCbor compiler subprocess for
building report entities that the workstation can serve to the
`@fxtr-ui/reports` frontend renderers.

Two halves:

- **Python package** (`fxtr_report_builder/`) — defines the
  `RootReport` and `SubReport` entity types and provides
  `build_mdx_tree` (low-level) + `build_root_report` /
  `build_sub_report` (high-level) helpers that compile MDX into
  the body fields.
- **TS subprocess** (`ts/packages/cli/`) — a self-contained
  `fxtr-report-builder-cli` workspace package that runs MDX
  through a custom JSX runtime + flatten pass, with no
  dependency on any `@fxtr-*` package. Bundled to a single
  `dist/main.cjs` via esbuild; invoked by Python over
  stdin/stdout with a CBOR-bytes payload.

## Pipeline

```
Python                                          TS subprocess
──────                                          ─────────────
RawWeakFxtrID inputs                            opaque CBOR Tags
   │ fxtr_cbor_dumps + base64                          ▲
   ▼                                                   │
JSON envelope ───────────── stdin ──────────────► @mdx-js/mdx
{ source, inputs, passthroughComponents }       + custom JSX runtime
                                                + flatten (assigns figureKey)
                                                        │
{ ok, treeCbor: base64 } ◄── stdout ─────────── re-encode CBOR
   │
   ▼
fxtr_cbor_loads → FrozenFxtrCbor → RootReport.contents
```

Ref-typed inputs cross the Python/TS boundary as raw CBOR bytes
with tags 33000 / 33001 / 33002. The TS side **never interprets
these tags** — they round-trip verbatim through MDX expansion
as opaque values, and the same `RawWeakFxtrID` instances
reappear at the same positions when Python decodes the response.

**Provenance is preserved across the boundary.** `fxtr_cbor_dumps`
only ships the bare `_raw_data_id` of each FxtrID, so the Python
side mirrors the preserve-prov persistence cap (see
`projects/fxtr_workstation/src/fxtr_workstation/persistence.py`): every
input FxtrID is projected to its bare provenance form before
encoding (`extract_provenance_parent` for layered IDs;
`create_u_node` for bare data IDs), and every FxtrID in the
decoded response tree is re-layered via `embed_as_provenance_parent`.
The active `FxtrScope` is captured at the call site via
`get_active_scope` and threaded through both halves. Bare
provenance-tag inputs (`n`/`c`/`p`/`u` with no provenance parent)
are rejected as ambiguous.

`RenderFigure` nodes in the flattened tree additionally get a
stable `figureKey: "figure-<i>"` prop assigned by the flatten
pass. The frontend tree renderer uses this key to project
per-figure storage atoms out of the containing report's panel
`ownStorage`.

## Directory structure

```
fxtr_report_builder/
├── CODE_OVERVIEW.md
├── pyproject.toml                    # uv-managed Python package
├── fxtr_report_builder/              # Python package
│   ├── __init__.py                   # public re-exports
│   ├── entities.py                   # RootReport, SubReport (DataclassEntity)
│   ├── mdx_builder.py                # build_mdx_tree (low-level) +
│   │                                 # build_root_report / build_sub_report (high-level)
│   └── _ts_dist_path.py              # resolve_report_cli_command (arg → FXTR_REPORT_BUILDER_CLI env → dev `node dist/main.cjs`) + resolve_ts_dist_path (dev bundle path) + MdxSubprocessError
├── tests/
│   ├── test_entities.py              # store/load round-trip
│   └── test_mdx_builder.py           # subprocess + MDX compile integration
└── ts/
    └── packages/
        └── cli/                      # standalone (NOT a pnpm-workspace member); own pnpm-lock.yaml
            ├── package.json          # name "fxtr-report-builder-cli" (unscoped)
            ├── tsconfig.json
            ├── esbuild.config.mjs    # bundles src/main.ts → dist/main.cjs
            ├── src/
            │   ├── main.ts           # stdin/stdout entry point
            │   ├── jsx_runtime.ts    # custom JSX → MdxNode tree
            │   ├── flatten.ts        # MdxNode → tuples + figureKey assignment
            │   ├── evaluate.ts       # MDX compile + run + flatten
            │   └── cbor_passthrough.ts  # tag-preserving cbor-x wrappers
            └── dist/                 # build artifact (gitignored)
```

## Building the TS subprocess

The CLI is **standalone — deliberately NOT a member of the repo-root
`pnpm-workspace.yaml`** (it has no `@fxtr-*`/`workspace:*` deps, only
`@mdx-js/mdx` + `cbor-x`). It keeps its own `pnpm-lock.yaml` so it can be built
pure-pnpm-in-nix via `lib.fxtr.reportBuilderCli` (`pnpm.fetchDeps` wants a
self-contained package + lockfile, not the whole-repo workspace closure). Build
it in its own dir, passing `--ignore-workspace` so pnpm uses the CLI's own lock
instead of walking up to the root workspace:

```bash
cd projects/fxtr_report_builder/ts/packages/cli
pnpm install --ignore-workspace
pnpm build              # runs `node esbuild.config.mjs`
```

Pre-commit keeps the standalone lock in sync + typechecks the CLI via the
dedicated `pnpm-report-cli` hook (`.pre-commit-config.yaml`); the nix build
(`lib.fxtr.reportBuilderCli`) is the production path.

The output is `ts/packages/cli/dist/main.cjs` — a single
self-contained file with `@mdx-js/mdx` + `cbor-x` inlined.

**How the CLI is located + invoked.** `_ts_dist_path.resolve_report_cli_command`
returns the launch argv in a generic order: an explicit `cli_path` arg (threaded
from `build_mdx_tree(..., cli_path=)` / `build_*_report`), then the
`FXTR_REPORT_BUILDER_CLI` env var, then the dev fallback. Cases 1/2 name a
**self-contained executable** (e.g. the nix-built `lib.fxtr.reportBuilderCli`
wrapper, node baked in) → run as `[exe]`; the dev fallback runs the in-tree
bundle as `["node", str(resolve_ts_dist_path())]` (raising `MdxSubprocessError`
with a build hint if it's missing). The env var is **generic wiring** — a
report-step builder sets it via its `env`; nix has no report-builder-specific
code.

## Python API

Entity types (define-then-store pattern, ambient scope at module
import time hosts the type entities — store them downstream in
whatever scope you like):

```python
from fxtr_report_builder import RootReport, SubReport, build_root_report

report = await build_root_report(
    title="My Report",
    summary="Short summary.",
    source="""
# Hello

<RenderFigure entity={props.fxtr_inputs.sub} />
""",
    inputs={"sub": some_sub_report_ref},   # EntityRef | FxtrID
)
ref = report.store(scope)
```

The high-level helpers (`build_root_report` / `build_sub_report`)
hardcode the `RenderFigure` / `RenderLink` passthrough
components and accept inputs in the broad shape
(`EntityRef[Any] | FxtrID | RawWeakFxtrID | RawStrongFxtrID`).
Internally they convert to `RawWeakFxtrID` before calling
`build_mdx_tree`.

`build_mdx_tree` is exposed for callers that want to compile a
tree without wrapping it in a particular entity type, or to
customize the passthrough-components list.

## Errors

- **`MdxBuildError`** — typed compile-time failure reported by
  the TS subprocess (MDX syntax error, invalid input). Raised
  through the `{ok: false, error: str}` response envelope.
- **`MdxSubprocessError`** — infra failure: missing
  `dist/main.cjs`, subprocess exited non-zero, malformed JSON
  response. Carries `stderr` + `returncode` for debugging.

## MDX conventions

- Ref-typed inputs are exposed under `props.fxtr_inputs.<name>`.
  Example: `<RenderFigure entity={props.fxtr_inputs.subReport} />`.
- The default passthrough components are `RenderFigure` and
  `RenderLink`. Custom names can be passed to
  `build_mdx_tree`'s `passthrough_components`.
- An explicit `figureKey="..."` prop on a `RenderFigure` node
  is respected as-is; auto-counter assignment is the fallback
  for un-keyed figures. This is a future-extension hatch — not
  documented in the high-level helpers.

## Entity shape (Python side)

```python
@dataclasses.dataclass(frozen=True)
class RootReport(DataclassEntity, fxtr_type=ROOT_REPORT_TYPE_ID):
    title: str
    summary: str
    contents: FrozenFxtrCbor

@dataclasses.dataclass(frozen=True)
class SubReport(DataclassEntity, fxtr_type=SUB_REPORT_TYPE_ID):
    label: str
    overview: FrozenFxtrCbor
    detail: FrozenFxtrCbor
```

The `FrozenFxtrCbor` field type is round-tripped through the
default `FrozenFxtrCborSerializationStrategy` registered on
`DataclassEntity.Config.serialization_strategy` — no per-class
override needed. The workstation's wire codec converts the
embedded `RawWeakFxtrID` leaves to `["entity", short_id]` wire
tags at load time, so the frontend tree renderer sees
`EntityRefStub` instances at ref positions in the loaded
payload.

## Frontend half

The frontend renderers live in
[`projects/fxtr_workstation/frontend_lib/packages/fxtr-ui-reports/`](../fxtr_workstation/frontend_lib/packages/fxtr-ui-reports/);
see that package's `LOCAL_CODE_OVERVIEW.md` for the rendering
model + the consumer-side dispatch-registry wiring.

## Tests

```bash
# Python tests (require TS dist to be built — see "Building" above):
uv run pytest tests/

# Direct subprocess smoke test (no Python):
echo '{"source":"# H\n","inputs":{},"passthroughComponents":["RenderFigure","RenderLink"]}' \
  | node ts/packages/cli/dist/main.cjs
```

The `test_mdx_builder.py` suite skips automatically with a
clear message if `dist/main.cjs` isn't present.
