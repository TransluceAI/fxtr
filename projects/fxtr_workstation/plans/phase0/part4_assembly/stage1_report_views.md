# Stage 1 — Report views as workstation panels

Build a new frontend dispatch package (`@fxtr-ui/reports`) and a
Python entity-authoring package (`fxtr_report_builder`) so report
entities can be authored in Python, with MDX content compiled via
a self-contained TS subprocess, and displayed as workstation
panels. Introduce a new `Figure` dispatch key for inline embedding
of entities inside report bodies, with a sensible default fallback
that re-dispatches to `FullPanel`.

Wire it through the existing `@fxtr-ui/workstation-demo` so the
end-to-end story is exercisable by `pnpm dev`.

## Goals

1. A workstation-shaped React package `@fxtr-ui/reports`
   contributing:
   - the wire-decoded data shapes `RootReportValue`,
     `SubReportValue` (typed views over the loaded entity
     payload — no entity-definition runtime, since the entity
     types live in Python)
   - the new `Figure` dispatch key (5th key alongside the
     workstation's four)
   - renderers for both report types: `RootReport` gets
     `FullPanel` / `BlockLink` / `InlineLink` / `HoverPreview`
     (no `Figure` renderer — embedding a `RootReport` inside
     another report isn't a supported pattern; users wanting
     a nested figure should reach for `SubReport`);
     `SubReport` gets all five keys including `Figure`
   - a tree renderer that walks the loaded MDX FxtrCbor tree and
     emits HTML/SVG elements + nested `Figure` / `InlineLink`
     dispatches for the embedded entity refs
   - a default `Figure` no-match fallback that wraps the entity's
     `FullPanel` in a shaded container with a `RefLink` header

   No `createReportsDispatchRegistry` helper — the demo (and any
   future consumer) constructs its dispatch registry manually
   via `createDispatchRegistry` from `@fxtr-ui/react-dispatch`,
   composing the workstation's four key entries with the new
   `Figure` key entries. The package exports the `Figure` key
   plus all five `staticEntityTypeDispatch` lookup-map shapes
   so consumers can build their registry concisely.

2. A Python package `fxtr_report_builder` at
   `projects/fxtr_report_builder/`:
   - `RootReport` / `SubReport` defined as
     `DataclassEntity` subclasses with body-tree fields typed
     `FrozenFxtrCbor` (relies on a globally-configured
     `FrozenFxtrCborSerializationStrategy` — see step under
     "Implementation steps")
   - a low-level `async build_mdx_tree(source, inputs, ...)`
     helper that shells out to a TS subprocess, ships ref inputs
     as CBOR bytes, and returns the compiled `FrozenFxtrCbor`
   - higher-level helpers `async build_root_report(...)` and
     `async build_sub_report(...)` that wrap the low-level
     helper: they hardcode the `RenderFigure` / `RenderLink`
     passthrough components, accept `inputs` as `EntityRef[Any]
     | FxtrID` and convert internally to `RawWeakFxtrID`, and
     return a fully-constructed entity ready to `.store(scope)`

3. A self-contained TS subprocess at
   `projects/fxtr_report_builder/ts/` that:
   - reads `{source, inputs}` from stdin (one JSON line)
   - compiles MDX through a forked-down `@fxtr/mdx-transform`
     pipeline (no `@fxtr/core` dependency)
   - preserves ref-tag CBOR values opaquely through the entire
     compile + flatten phase
   - re-encodes the flattened tree as CBOR and writes the bytes
     back via stdout (one JSON line with base64)

4. Demo wiring: the existing `@fxtr-ui/workstation-demo` gets two
   new entities (one `RootReport` and one `SubReport`) wired into
   `expose_entity`, with the report's contents embedding a
   `RenderFigure` of one of the existing demo entities (e.g. a
   `_DemoNote`) to exercise the no-match-figure path. The TS
   client registers the new package's renderers.

## Non-goals (this stage)

- `SimpleComment` and any margin-note / sidebar UI — the old
  system's sidebar concept doesn't map cleanly onto the
  workstation's single-panel chrome and is deferred.
- `LinkedSVGFigure` — the bar-chart-with-clickable-bars
  interaction is out of scope; the demo embeds an existing demo
  entity through `RenderFigure` instead.
- URL sync / breadcrumb UI inside reports — the workstation
  shell already owns panel-level breadcrumbs and chain history.
- Navigation actions inside reports (e.g. clicking a refLink in
  the rendered tree) reuse the workstation's `RefLink` machinery
  — no report-specific navigation reducer.

## Architecture overview

### Data flow

```
Python                                 TS subprocess
──────                                 ─────────────
RawWeakFxtrID inputs                   inputs as opaque CBOR Tags
   │                                          │
   │ fxtr_cbor_encode_hook                    │
   ▼                                          ▼
JSON envelope ───────── stdin ────────► @mdx-js/mdx + custom
{ source, inputs:                       JSX runtime + flatten
  base64(cbor_bytes) }                         │
                                               ▼
                                        FxtrCbor tree (refs are
                                        Tag passthroughs)
                                               │
{ ok, treeCbor: base64 } ◄─── stdout ──────── re-encode CBOR
   │
   ▼
fxtr_cbor_loads → FrozenFxtrCbor → store in RootReport.contents
```

The workstation's wire codec, when the report is loaded on the
frontend, turns each `RawWeakFxtrID` leaf in the CBOR-decoded
contents into an `["entity", short_id]` tag, which the frontend
`unescapeWire` resolves into an `EntityRefStub` interned on the
connector. The tree renderer never sees the raw cbor — it walks
plain JS arrays/objects with `EntityRefStub` leaves.

### Frontend package layout

```
frontend_lib/packages/fxtr-ui-reports/
├── package.json
├── tsconfig.json
└── src/
    ├── index.ts
    ├── keys.ts                # Figure dispatch key + FigureProps
    ├── fallbacks.tsx          # FigureNoMatchFallback (shaded FullPanel + RefLink)
    ├── types.ts               # RootReportValue, SubReportValue (wire shapes)
    ├── tree_renderer.tsx      # walks loaded FxtrCbor tree → React
    ├── figure_storage.ts      # narrows FullPanel ownStorage → per-figure sub-atoms
    └── handlers/
        ├── RootReport.tsx
        └── SubReport.tsx
```

Workspace-level: this is a new package in the `v2/ts` pnpm
workspace (sibling of the existing `@fxtr-ui/*` packages). No
`@fxtr/*` dependencies; only `@fxtr-ui/*` workspace links + react
+ jotai peers.

### Python package layout

```
projects/fxtr_report_builder/
├── CODE_OVERVIEW.md           # written once the package is in shape
├── pyproject.toml             # uv-managed Python package
├── fxtr_report_builder/
│   ├── __init__.py            # re-export RootReport, SubReport,
│   │                          # build_mdx_tree, build_root_report,
│   │                          # build_sub_report
│   ├── entities.py            # DataclassEntity defs
│   ├── mdx_builder.py         # low-level subprocess wrapper +
│   │                          # high-level build_*_report helpers
│   └── _ts_dist_path.py       # locate ts/dist/main.cjs at runtime
├── ts/
│   └── packages/
│       └── cli/               # workspace member, self-contained
│           ├── package.json   # name "fxtr-report-builder-cli" (unscoped)
│           ├── tsconfig.json
│           ├── esbuild.config.mjs   # bundles src/main.ts → dist/main.cjs
│           ├── src/
│           │   ├── main.ts          # stdin/stdout entrypoint
│           │   ├── jsx_runtime.ts   # forked from @fxtr/mdx-transform
│           │   ├── flatten.ts       # ditto; assigns figureKey to RenderFigure
│           │   ├── evaluate.ts      # ditto
│           │   └── cbor_passthrough.ts  # Tag-preserving CBOR layer
│           └── dist/                # build artifact (gitignored); contains main.cjs
└── tests/
    ├── test_entities.py       # round-trip store/load
    └── test_mdx_builder.py    # MDX → cbor → entity → wire-shape integration
```

`ts/packages/cli/` is a workspace member of the `v2/ts` pnpm
workspace (added to `pnpm-workspace.yaml` matching the
existing `projects/fxtr_workstation/ts/packages/*` glob style).
`pnpm install && pnpm build` from `v2/ts/` runs esbuild to
produce `dist/main.cjs` — a single self-contained file with
`@mdx-js/mdx` + `cbor-x` bundled in. Python invokes it as
`node <ts/packages/cli/dist/main.cjs>`.

The TS package name is unscoped (`fxtr-report-builder-cli`) —
no association with any `@fxtr-*` namespace, since it's a tool
package and doesn't share an API surface with the runtime
libraries.

## Component breakdown

### 1. `@fxtr-ui/reports` — the `Figure` key

```ts
// keys.ts
export interface FigureProps {
  readonly ownStorage: FxtrSessionAtom<unknown>;
}

export const Figure: DispatchKey<FigureProps> = declareDispatchKey<FigureProps>({
  uuid: /* fresh RFC 4122 uuid */,
  description: "Figure",
  fallbacks: {
    Loading: FigureLoadingFallback,        // small skeleton box
    NoMatch: FigureNoMatchFallback,        // the FullPanel-wrap fallback
    Error: castErrorFallback<FigureProps>(),
  },
});
```

The `Figure` key's `Props` includes `ownStorage` for the same
reason `FullPanel` does: the no-match fallback re-dispatches to
`FullPanel`, which needs storage to thread through.

### 2. The default `FigureNoMatchFallback`

```tsx
// fallbacks.tsx (pseudocode)
const FigureNoMatchFallback = ({stub, ownStorage}) => (
  <div className="figure-fallback-shaded">
    <div className="figure-fallback-header">
      <RefLink stub={stub} as="block" />
    </div>
    <RenderDispatch as={FullPanel} stub={stub} ownStorage={ownStorage} />
  </div>
);
```

Visual style: a faint surface background (`var(--surface-sunken)`
or similar from the workstation tokens), inner padding, and a
small caption row above the inner panel rendering carrying the
`RefLink`. The caption signals "this is figure X embedded in a
report" so the user can navigate to the full ref. Component lives
in `fallbacks.tsx`; styles in the workstation CSS-token palette.

### 3. The tree renderer

```ts
// tree_renderer.tsx
interface ReportTreeRendererProps {
  document: readonly unknown[];   // wire-decoded FxtrCbor — array of tuples
  figureStorage: (figureKey: string) => FxtrSessionAtom<unknown>;
}
```

Walks each tuple `[type, props, children]`. Element-type handling:

- `"__fragment"` — flatten children inline.
- HTML element names (`"h1"`, `"p"`, `"div"`, etc.) and SVG element
  names — `createElement(type, props, ...children)` with children
  recursively rendered.
- `"RenderFigure"` — `props.entity` is an `EntityRefStub`,
  `props.figureKey` is a stable string assigned by the MDX
  builder (e.g. `"figure-0"`, `"figure-1"`, …); render
  `<RenderDispatch as={Figure} stub={entity}
     ownStorage={figureStorage(figureKey)} />`.
- `"RenderLink"` — `props.entity` is an `EntityRefStub`,
  `props.linkText` is an optional string; render
  `<RefLink stub={entity} as="inline" linkText={linkText} />`.
- Unknown element names — render a fallback placeholder (same
  styling as the old system's "unregistered component" dashed
  box).

The HTML/SVG element-name allowlists are copied verbatim from the
old `treeRenderer.tsx`. No `data-preview-target` interactivity
this stage (was tied to `LinkedSVGFigure`).

The renderer is purely functional / non-suspending — all entity
loads happen inside the dispatched `RenderDispatch` machinery, so
the tree renderer never blocks on async work.

### 4. Figure storage projection

The report's `FullPanel` renderer receives `ownStorage:
FxtrSessionAtom<unknown>` from `PanelWrapper`. It narrows that to:

```ts
interface RootReportPanelStorage {
  readonly figures: Record</* figureKey */ string, unknown>;
}
```

via `atomWithDefault` and threads a `figureStorage(figureKey)`
lookup into the tree renderer. The lookup constructs a sub-atom
via `subAtom(figuresAtom, figureKey)` (interpreting it as a
`Record<string, unknown>` key). Identity-stable across renders as
long as the parent atoms are memoized.

Figure keys are assigned by the MDX builder during the flatten
pass (see "TS subprocess" below). Each `RenderFigure` node gets
a `figureKey: "figure-<i>"` baked into its `props`, where `<i>`
is an incrementing counter scoped to one `build_mdx_tree` call.
The frontend just reads the key; the figure's `EntityRefStub`
identity is independent of the storage slot.

Consequences:
- A given figure's storage is stable across re-mounts and across
  tree-renderer remounts.
- Two `RenderFigure` references to the same entity get distinct
  storage slots (because their `figureKey`s differ).
- Adding / removing figures earlier in the source shifts the
  later figures' auto-assigned keys, so prior state for them
  resets. Acceptable trade-off; future work can grow named-figure
  support by letting the MDX source pass a `name="..."` prop that
  overrides the auto-counter, but that's out of scope for stage 1.

### 5. Report handlers

`RootReport` (entity-side handlers; `RootReportValue` typed
shape):

```ts
interface RootReportValue {
  readonly title: string;
  readonly summary: string;
  readonly contents: readonly unknown[];   // FxtrCbor tuple tree
}
```

- `FullPanel` — load entity, narrow ownStorage to
  `RootReportPanelStorage`, render `<h1>{title}</h1>` + `<p
  className="report-summary">{summary}</p>` + `<ReportTreeRenderer
  document={contents} figureStorage={...} />`.
- `BlockLink` — load entity, render a pill with tag `"report"`
  and the title.
- `InlineLink` — load entity, render an inline span with the
  title (or `linkText` override).
- `HoverPreview` — load entity, render a small card with title +
  summary excerpt.
- No `Figure` renderer — see the goals section. If a `RootReport`
  is ever embedded via `RenderFigure`, the default
  `FigureNoMatchFallback` triggers (shaded `FullPanel` + reflink
  header). In practice consumers should use `SubReport` for
  figures.

`SubReport` handlers: same five keys, with `SubReportValue` =
`{label, overview, detail}`:

- `FullPanel` — label heading + tree-render `overview`, then
  tree-render `detail` below it.
- `BlockLink` / `InlineLink` — label as the link text.
- `HoverPreview` — label + tree-render `overview`.
- `Figure` — label as small heading + tree-render `overview`
  (no `detail`). This is the "figure-mode" rendering — what
  the old `FigureBlock` did.

### 6. Registry composition (consumer-side)

No `createReportsDispatchRegistry` helper. The new package exports
the `Figure` key, the `FigureProps` type, and the `RootReport` /
`SubReport` renderers. Consumers compose their dispatch registry
manually via `createDispatchRegistry` from
`@fxtr-ui/react-dispatch`, building entity/service entries for
both the workstation's four keys (constructed via the existing
`createWorkstationDispatchRegistry` for the parts they want
default behavior, OR via raw `entityEntry` / `serviceEntry` calls
for full control) plus the report package's `Figure` key.

The demo's `dispatch_registry.tsx` (see "Demo wiring") is the
canonical example.

### 7. Python entities

```python
# fxtr_report_builder/entities.py
@dataclasses.dataclass(frozen=True)
class RootReport(DataclassEntity, fxtr_type=_ROOT_REPORT_TYPE_ID):
    title: str
    summary: str
    contents: FrozenFxtrCbor
```

Same shape for `SubReport` with fields `label`, `overview`,
`detail`. Namespace `_FXTR_REPORTS_NS` is a fresh hardcoded UUID
(no relation to the old TS-side `"fxtr-reports"` namespace; this
is a new layer of entities owned by Python).

No per-subclass `Config` override needed: `FrozenFxtrCbor` is
added to `DataclassEntity.Config.serialization_strategy`
globally as part of this stage (see implementation step 2
below). The serialization strategy itself already exists in
`fxtr2/conversion/converters/mashumaro_entities.py`; it's just
not currently registered in the default dispatch map. Doing it
globally means any future entity that wants opaque tree data
can declare a `FrozenFxtrCbor` field without ceremony.

### 8. Python MDX builder

Two layers — a low-level helper for raw `FrozenFxtrCbor` output
and high-level helpers that return ready-to-store entities.

**Low-level:**

```python
async def build_mdx_tree(
    source: str,
    *,
    inputs: Mapping[str, RawWeakFxtrID | RawStrongFxtrID] = {},
    passthrough_components: Sequence[str] = ("RenderFigure", "RenderLink"),
) -> FrozenFxtrCbor:
    """Compile MDX `source` to a FxtrCbor tree.

    `inputs` maps a name (referenced as
    `props.fxtr_inputs.<name>` inside the MDX source) to a raw
    ref. Raw refs round-trip through the TS subprocess as
    opaque CBOR tags and survive the MDX expansion intact — the
    resulting tree contains the same RawWeakFxtrID instances at
    the positions where they're embedded.

    `passthrough_components` lists JSX element names that should
    flatten to `[name, props, []]` tuples (so the frontend tree
    renderer can dispatch on them). RenderFigure additionally
    gets a stable `figureKey` assigned by the TS flatten pass.
    """
    ...
```

**High-level helpers:**

```python
async def build_root_report(
    *,
    title: str,
    summary: str,
    source: str,
    inputs: Mapping[str, EntityRef[Any] | FxtrID] = {},
) -> RootReport: ...

async def build_sub_report(
    *,
    label: str,
    overview_source: str,
    detail_source: str,
    overview_inputs: Mapping[str, EntityRef[Any] | FxtrID] = {},
    detail_inputs: Mapping[str, EntityRef[Any] | FxtrID] = {},
) -> SubReport: ...
```

These convert `EntityRef[Any] | FxtrID` to `RawWeakFxtrID`
internally, hardcode `passthrough_components=("RenderFigure",
"RenderLink")`, call `build_mdx_tree`, and construct the entity.
Consumers do `(await build_root_report(...)).store(scope)`.

**Implementation:** encode each `RawWeakFxtrID` input via
`fxtr_cbor_dumps(input)`, base64; ship a JSON envelope through
stdin to `node <ts/dist/main.cjs>`; read JSON envelope from
stdout; base64-decode the response's `treeCbor` field;
`fxtr_cbor_loads`; freeze and return.

Subprocess invocation: uses `anyio.open_process`. Two-tier
error surface:

- `MdxBuildError` — raised when the TS side reports a typed
  failure via the response envelope's `{ok: false, error:
  str}` path (e.g. MDX compile errors, invalid input CBOR).
  Treat as a user error.
- `MdxSubprocessError` — raised when the subprocess itself
  misbehaves: non-zero exit code without a valid response,
  malformed JSON on stdout, missing `dist/main.cjs`, etc.
  Carries the captured stderr and exit code for debugging.
  Treat as an infra error.

`_ts_dist_path.py` resolves the bundled JS at runtime —
`dist/main.cjs` ships under the TS subpackage. We find it via
`pathlib.Path(__file__).parent / ".." / "ts" / "packages" /
"cli" / "dist" / "main.cjs"` (relative to
`fxtr_report_builder/_ts_dist_path.py`). If the file is
missing, the helper raises `MdxSubprocessError` with a
message instructing the user to run `pnpm install && pnpm
build` from `v2/ts/`.

### 9. TS subprocess

Pipeline mirrors the old `@fxtr/mdx-transform`:

1. `main.ts` — read one JSON line from stdin. Decode each
   `inputs[name]` via base64 → bytes → CBOR via `cbor-x`'s
   `Decoder` configured for tag passthrough (unknown tags
   surface as `cbor-x` `Tag` instances). The resulting object
   tree contains primitives, arrays, objects, and `Tag`
   instances at ref positions.

2. `evaluate.ts` — assemble a `props` object whose keys match
   `inputs`'s keys; values are the deserialized `Tag` instances
   (treated as opaque objects by the MDX runtime). Compile the
   MDX source via `@mdx-js/mdx`'s `compile` + `run`, threading
   the custom JSX runtime + `props.fxtr_inputs = <decoded
   inputs>`. (The MDX source references inputs as
   `props.fxtr_inputs.svgFigure`, etc.)

3. `jsx_runtime.ts` — produce `MdxNode` instances with full
   prop validation, but the "FxtrCbor-serializable" predicate
   accepts any value (including `Tag` instances) as long as
   it's not a function or a React element. Forked-down from
   the existing `@fxtr/mdx-transform`'s rules with
   `RawWeakFxtrID` replaced by `Tag`-typed pass-through.

4. `flatten.ts` — convert `MdxNode` tree → tuple form `[type,
   props, children]`, awaiting async components. **During this
   pass, an incrementing counter assigns a stable
   `figureKey: "figure-<i>"` to each `RenderFigure` node's props
   (counter scoped to one `build_mdx_tree` call).** Only
   `RenderFigure` gets a key; `RenderLink` doesn't need one
   (links carry no state).

5. `cbor_passthrough.ts` — re-encode the resulting tuple tree
   via `cbor-x`'s `Encoder` configured to write through `Tag`
   instances verbatim. Output is bytes; main.ts base64s and
   writes the JSON line response.

6. Custom passthrough components — for each name in
   `passthroughComponents`, register a JS function that emits
   a tuple-shaped node with that name. (Equivalent to the old
   `passthroughCustomComponent`.)

Stdout protocol: precisely one JSON line on success, one JSON
line on error. Any non-JSON stderr output is for human
debugging only; the Python side captures it for error
reporting.

### 10. Demo wiring

Backend (`projects/fxtr_workstation/frontend_lib/packages/fxtr-ui-workstation-demo/src/demo_workstation.py`):

- Add `fxtr_report_builder` as a dev/runtime dep (path dep).
- After the existing entity construction, build:
  - A `SubReport` `methodology_sub` via `build_sub_report` whose
    `overview` is a tiny MDX paragraph and `detail` is a longer
    one with no refs.
  - A `RootReport` `demo_report` via `build_root_report` with
    `inputs={"note": note_refs[0], "sub": methodology_sub_ref}`
    (high-level helper accepts `EntityRef[Any] | FxtrID`
    directly). The MDX source embeds:
    ```mdx
    # Demo Report

    Some prose.

    <RenderFigure entity={props.fxtr_inputs.sub} />

    More prose with a <RenderLink entity={props.fxtr_inputs.note} linkText="related note" />.
    ```
- Expose under names `demo/report` and `demo/sub-report`.
- Add `report` and `sub_report` keys to the `TYPE_IDS` JSON
  emitted on stdout.

Frontend (`projects/fxtr_workstation/frontend_lib/packages/fxtr-ui-workstation-demo/src/client/dispatch_registry.tsx`):

- Construct the dispatch registry manually via
  `createDispatchRegistry` (from `@fxtr-ui/react-dispatch`).
  Entity entries cover the workstation's four keys
  (`FullPanel`, `BlockLink`, `InlineLink`, `HoverPreview`) plus
  the report package's `Figure` key — all using
  `staticEntityTypeDispatch`. Service entries cover the four
  workstation keys via `staticServiceTypeDispatch` (no `Figure`
  on the service side — services aren't expected as figures).
- The lookup maps include the new report renderers from
  `@fxtr-ui/reports` for all applicable keys, alongside the
  existing `DemoNote` / `DemoListNode` / service entries.
- The note renderer (existing `DemoNoteFullPanel`) is NOT
  registered for the `Figure` key — so when the report's tree
  includes a `RenderFigure entity={note}`, the dispatcher falls
  through to `FigureNoMatchFallback`, which renders the note's
  `FullPanel` wrapped in the shaded container. This is the
  user-visible demonstration of the default fallback.

### 11. Tests

Frontend:
- `frontend_lib/packages/fxtr-ui-reports/tests/tree_renderer.test.tsx`
  — drives the tree renderer against synthesized tuple trees:
  HTML/SVG elements, `RenderFigure` with a `figureKey` (calls
  a stubbed `RenderDispatch` and asserts `ownStorage` was
  derived through the supplied `figureStorage(figureKey)`),
  `RenderLink` (calls a stubbed `RefLink`), `__fragment`
  unwrap, unknown-element placeholder.
- `frontend_lib/packages/fxtr-ui-reports/tests/figure_storage.test.tsx`
  — confirms `figureStorage(figureKey)` returns a stable
  `FxtrSessionAtom` per key across calls.
- Type-level tests for `Figure` Props symmetric with the
  workstation key tests.
- (Render-layer tests for the handler components — if any —
  follow the existing sibling-tests-package pattern in
  `v2/ts/packages/fxtr-ui-react-dispatch-tests/`; skip if
  exercise-by-demo is sufficient.)

Python:
- `projects/fxtr_report_builder/tests/test_entities.py` — round-
  trip `RootReport` + `SubReport` through a `FxtrScope` (store
  → load), assert ref leaves survive.
- `projects/fxtr_report_builder/tests/test_mdx_builder.py` —
  invoke `build_mdx_tree` against a small MDX source with one
  ref input, assert the returned `FrozenFxtrCbor` contains the
  expected `RawWeakFxtrID` at the expected position. Requires
  the TS dist to be built; the test guards with a clear
  skipif/message if `dist/main.cjs` is absent.

Workstation integration: the existing
`fxtr-ui-workstation-demo` package's `pnpm dev` is the smoke
test. No automated playwright-style assertion this stage;
manual "load the page, click around, see the figures fallback
correctly" is the deliverable.

## Open questions / risks

- **Custom MDX props convention.** The user MDX references
  inputs as `props.fxtr_inputs.<name>`. The TS evaluate.ts
  passes `{fxtr_inputs: <decoded>}` as the `props` argument
  to the compiled MDX. We could alternatively flatten and
  pass `{<name>: <decoded>, ...}` directly, mirroring the
  old `@fxtr/example-demo-report` server which used
  `props.svgFigure` etc. Going with the nested-under
  `fxtr_inputs` form to avoid colliding with `@mdx-js/mdx`'s
  built-in `props` keys (e.g. `components`).

- **CBOR library on TS.** Using `cbor-x` because it has tag
  passthrough and is small. Alternatives are `@stablelib/cbor`,
  `cbor-js`. Locking in `cbor-x` for now; if it proves
  awkward, swap is local to `cbor_passthrough.ts`.

- **Subprocess startup cost.** Each `build_mdx_tree` spawns a
  fresh `node` process (one-shot). For demo it's fine; if
  reports need to be built en masse we'd want a long-lived
  subprocess. Out of scope for stage 1.

- **The shared FullPanel storage on the Figure fallback path.**
  When a Figure falls back to FullPanel, the embedded panel
  reads the same storage atom the Figure was given. If the
  figure renderer later starts being registered (replacing
  the fallback), the storage layout it expects may not match
  the prior FullPanel's. This is the same hazard `PanelWrapper`
  already has via the per-entry display-mode invariant
  (metadata vs default never coexist for one entry). For
  Figure→FullPanel the contract is: the storage slot's lifetime
  is "while this figure occurrence is rendering"; switching
  the registered renderer is a development-time concern that
  resets state anyway.

## Implementation steps

(Steps 1-16 complete; step 17 is optional and rolls into stage 2
follow-ups noted at the bottom of this file.)

1. [x] **Scaffolding.**
   - Create `frontend_lib/packages/fxtr-ui-reports/` with
     `package.json`, `tsconfig.json`, `src/index.ts`.
   - Add the package to `pnpm-workspace.yaml` (additive;
     the workspace already enumerates frontend_lib paths).
   - Create `projects/fxtr_report_builder/` Python package with
     `pyproject.toml`, `fxtr_report_builder/__init__.py`,
     entity stubs.
   - Create `projects/fxtr_report_builder/ts/packages/cli/` with
     `package.json` (name `fxtr-report-builder-cli`),
     `tsconfig.json`, `esbuild.config.mjs`, `src/main.ts`
     skeleton.
   - Add `projects/fxtr_report_builder/ts/packages/*` to
     `pnpm-workspace.yaml` (matching the existing
     `projects/fxtr_workstation/ts/packages/*` glob style).
   - Run `pnpm install` once to confirm workspace links resolve.

2. [x] **Global `FrozenFxtrCbor` serialization-strategy registration.**
   Add `FrozenFxtrCbor: FrozenFxtrCborSerializationStrategy()` to
   the `serialization_strategy` map on `DataclassEntity.Config`
   in `v2/fxtr2/conversion/converters/mashumaro_entities.py`.
   Add a short unit test in `v2/tests/` covering round-trip of a
   simple `DataclassEntity` subclass with a `FrozenFxtrCbor`
   field carrying an embedded `RawWeakFxtrID`. Update
   `v2/CODE_OVERVIEW.md` if its description of the
   `DataclassEntity` strategy map enumerates the entries.

3. [x] **Python entity types.** Implement
   `fxtr_report_builder/entities.py` with `RootReport` and
   `SubReport`. Write `tests/test_entities.py` round-trip
   tests without any MDX involvement (manually-constructed
   `FrozenFxtrCbor` literals with embedded `RawWeakFxtrID`s).
   Run the tests; commit.

4. [x] **TS subprocess: stdin/stdout shell.** Land `main.ts` with
   the JSON envelope read/write, plus `cbor_passthrough.ts`
   wrapping `cbor-x`. Implement a degenerate "echo" mode first
   that just decodes inputs and re-encodes them as the tree,
   verifying the tag passthrough works end-to-end.

5. [x] **Python ↔ TS plumbing.** Implement `mdx_builder.py`'s
   low-level `build_mdx_tree` against the echo-mode
   subprocess. Write a test in `tests/test_mdx_builder.py`
   that ships in `RawWeakFxtrID(...)` inputs and asserts they
   come back intact (no MDX yet). Run; commit.

6. [x] **TS MDX pipeline.** Port `jsx_runtime.ts`, `flatten.ts`,
   `evaluate.ts` from `@fxtr/mdx-transform`, stripping
   `@fxtr/core` references and replacing them with `cbor-x`
   `Tag` types. In `flatten.ts`, assign each `RenderFigure`
   node's props a `figureKey: "figure-<i>"` via an
   incrementing counter scoped to the call. Wire `main.ts` to
   call `evaluateMdx` when the request envelope has a
   non-empty `source`. Extend the Python test to cover a real
   MDX compile + ref embed + figureKey presence.

7. [x] **High-level Python helpers.** Implement
   `build_root_report` and `build_sub_report` in
   `mdx_builder.py`. Test that they convert `EntityRef[Any] |
   FxtrID` inputs correctly and produce well-formed entities.

8. [x] **Frontend `Figure` key + fallbacks.** Land `keys.ts`,
   `fallbacks.tsx`. Type tests pass; no runtime tests yet.

9. [x] **Frontend tree renderer + figure storage.** Land
   `tree_renderer.tsx` + `figure_storage.ts` with their unit
   tests. Threads `figureKey` (read from `RenderFigure` node
   props) through to the storage lookup. Use existing
   `_mocks.ts` patterns from the dispatch-tests package for
   stubbed `RenderDispatch` / refs.

10. [x] **Frontend handlers.** Land
    `handlers/RootReport.tsx` and `handlers/SubReport.tsx`.

11. [x] **Per-package `LOCAL_CODE_OVERVIEW.md`** for
    `@fxtr-ui/reports`. Update top-level
    `frontend_lib/CODE_OVERVIEW.md` to mention the new package.

12. [x] **CODE_OVERVIEW.md for `fxtr_report_builder`.** Write
    `projects/fxtr_report_builder/CODE_OVERVIEW.md` (mirrors the
    convention used elsewhere — file listing, architecture
    summary, "how the TS subprocess is built and invoked"
    section). Update `projects/fxtr_workstation/CODE_OVERVIEW.md`
    or the root `CODE_OVERVIEW` index if it lists sibling
    packages.

13. [x] **Demo backend wiring.** Add `fxtr_report_builder` as a
    path dep of the demo package's pyproject. Extend
    `demo_workstation.py` to build the report + sub-report
    entities via `build_root_report` / `build_sub_report`,
    expose them under stable names, and emit the new type-id
    strings.

14. [x] **Demo frontend wiring.** Rewrite
    `dispatch_registry.tsx` to construct the registry manually
    via `createDispatchRegistry`. Add entity entries for the
    five keys (workstation 4 + Figure) and service entries for
    the four workstation keys, using the demo's existing
    component maps + the new report renderers. Leave the
    `_DemoNote` Figure slot intentionally unregistered so the
    fallback path is visible.

15. [x] **Hand-test via `pnpm dev` from the demo package.**
    Confirm:
    - opening `demo/report` shows the full report with the
      sub-report embedded as a `Figure` (custom rendering with
      label + overview).
    - the embedded `_DemoNote` reference shows the fallback —
      shaded container + `RefLink` header + nested
      `DemoNoteFullPanel`.
    - clicking the `RefLink` for the note navigates the panel
      (workstation behavior, unchanged).
    - reload preserves the report's panel state (figureKey
      storage round-trips through `attachSession`).

16. [x] **Update CODE_OVERVIEW.md** files: top-level
    `projects/fxtr_workstation/CODE_OVERVIEW.md` if the demo's
    architecture surface changed beyond a single new dep;
    the demo's own `LOCAL_CODE_OVERVIEW.md` (if present) /
    inline comments in `demo_workstation.py`.

17. [x] **(Optional)** A short follow-up note in
    `projects/fxtr_workstation/plans/phase0/part4_assembly/` if
    we identify anything worth tracking for a stage 2 (e.g.
    `SimpleComment`, the long-lived subprocess optimization,
    margin-note UI, `LinkedSVGFigure` revival, MDX hot-reload
    in dev, named figure keys overriding the auto-counter).
    Update `CODE_OVERVIEW.md` to reference it.

## Notes from implementation (worth tracking)

- **Top-level fragment extraction is required.** First pass left
  the top-level `__fragment` in the stored body and tried to
  pattern-match it at render time (`isTuple`); that was a code
  smell — it papered over loose typing at the producer/consumer
  boundary. The fix was to extract fragment children in the TS
  subprocess (`evaluate.ts`'s `extractFragmentChildren`) so the
  body shape is always a `readonly MdxChild[]` array. The
  frontend then narrows via control flow on `MdxChild`'s
  recursive union (in `types.ts`) without any runtime tuple
  predicate. Lesson: when a wire shape ambiguity tempts you to
  reach for a runtime predicate, fix the producer instead.

- **`useEntityLoad` was an antipattern.** Initial draft was a
  React hook that monopolized `useDispatchSharedAtom` for a
  load slot. That made composing additional shared state
  (figure storage for the hover preview) impossible without
  clobbering. Replaced with a pure-jotai
  `makeLoadResultAtom(slotAtom, stub)` helper: caller picks
  where the slot lives, the atom drives the effect + projects
  to a `LoadResult<T>` union. Renderers consume one
  `useAtomValue` + branch on `result.kind`.

- **Canonical renderer pattern.** Each renderer's `useMemo`
  spins up its own effect + reader atom + per-child storage
  closures, with module-level helpers only where the logic is
  truly straight-line-identical across renderers
  (`makeLoadResultAtom`, `makeFigureStorage`). Worth
  considering as a project-wide convention if other dispatched
  renderers grow similar shapes.

- **Per-figure storage key stability.** Storage keys are
  assigned by the TS subprocess (`figure-<i>` counter scoped
  to one `build_mdx_tree` call). Stable across panel re-mounts
  AND across panel-session attach/reattach; NOT stable across
  MDX-source edits — adding a figure earlier shifts the
  later figures' keys. Documented in `LOCAL_CODE_OVERVIEW.md`.

- **`SubReportFigure` and `FigureNoMatchFallback` share visual
  chrome.** Both wrap content in a shaded shell + uppercase tag
  + inline `RefLink` header. Styles live in
  `handlers/shared.tsx` so the two stay consistent. If we add
  more figure renderers later (e.g. a chart figure), they
  should reuse the same shell.

## Future stages (out of scope here)

- **Stage 2 (sketch).** `SimpleComment` + margin-note UI —
  requires designing how the FullPanel reserves a sidebar
  region without conflicting with the workstation shell. Could
  ship as a `<ReportFullPanel sidebar={...}>` opt-in shape
  consumers can compose.
- **Stage 3 (sketch).** Revive `LinkedSVGFigure` with a
  workstation-friendly click model — interactive SVG regions
  whose click triggers `WorkstationApi.followLink`.
- **Stage N.** Long-lived MDX-builder subprocess + watch mode
  for dev iteration. Reuses the same JSON envelope protocol
  with one message per build. Each `build_mdx_tree` call
  currently spawns a fresh `node` process (~50-200ms startup
  per call) — fine for demos, slow for bulk report building.
- **Named figure keys.** The TS flatten pass honors an explicit
  `figureKey="..."` prop if the MDX source supplies one,
  falling back to the auto-counter. Currently undocumented;
  future stages can document this as a way to get
  edit-stable keys when figures are reordered.
- **`RootReport`-as-figure renderer.** Currently embedding a
  `RootReport` inside another report's body falls through to
  `FigureNoMatchFallback` (the entity's `FullPanel` framed
  as a figure). If nested reports become common, a dedicated
  `RootReportFigure` renderer (compact title + summary,
  reflink to open the full report in a new panel) is worth
  considering.
- **MDX layering refactor.** `@fxtr-ui/reports` depends on
  `@fxtr-ui/workstation` directly (for `FullPanel` /
  `RefLink`). Mentioned during planning as a known layering
  that a future refactor may invert or split. Not urgent.
