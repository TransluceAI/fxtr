# Step 3: Example React Web Client

## Goal

Add a browser-based React UI to the `@fxtr/examples` package that connects to an fxtr HTTP server and renders entities interactively. This serves as both a demo and a validation of the `@fxtr/react` API design.

## What it should do

Connect to a running fxtr HTTP server (the existing `pnpm --filter @fxtr/examples server`), browse pins, and render entity trees with cascading loads — the same operations as the existing CLI `client.ts` example, but as a web UI.

Feature set:

- **Pin browser**: list all pins from the server, optionally filtered by prefix. Click a pin to view its entity.
- **View mode toggle**: a checkbox to switch between "rich view" (typed `ArticleView` etc.) and "raw view" (`UnknownEntityView`). Both views use `useFxtrLoad` — the difference is the codec (typed vs `unknownLoadCodec`).
- **ArticleView** (rich mode): structured rendering of Article entities with cascading loads — title, body, author badge, tag list. Each child component uses `useFxtrLoad` independently, demonstrating the Suspense pattern.
- **UnknownEntityView** (raw mode): a collapsible pretty-printed CBOR tree. Any subtree (object, array) can be folded to a one-line summary. When the tree contains a `FxtrID`, it's rendered as a collapsed badge showing the ID string; expanding it triggers a `useFxtrLoad` (via `unknownLoadCodec`) to fetch and inline that entity's full CBOR tree.
- **Cache isolation**: wrap the entity viewer in `<FxtrScope>` so navigating away from an entity cleans up its tracked data.

## Approach

Lightweight client-side React app. No SSR, no build framework complexity. Vite for dev server + bundling.

### New files

```
packages/examples/
├── index.html              # Vite entry point
├── vite.config.ts
├── src/
│   ├── entities.ts         # (existing) shared entity definitions
│   ├── server.ts           # (existing) server example
│   ├── client.ts           # (existing) CLI client example
│   └── web/
│       ├── main.tsx        # React entry point: createRoot, FxtrProvider setup
│       ├── App.tsx         # Top-level layout, HTTP namespace setup, view mode toggle
│       ├── PinBrowser.tsx  # Lists pins, handles selection
│       ├── ArticleView.tsx # Rich typed view: Article, AuthorBadge, TagBadge
│       └── UnknownEntityView.tsx  # Collapsible CBOR tree viewer
```

### Key integration points

**Setup (`main.tsx` / `App.tsx`)**: Use `createHttpReadNamespace` from `@fxtr/http-client` (added in step 1) to create an HTTP-backed namespace. Pass `ns.scope` to `<FxtrProvider>`. Clean up via `ns.scope.scheduleClose()` when the app unmounts.

**Pin browsing (`PinBrowser.tsx`)**: Uses `fetch` directly to call `/pins?prefix=...` (returns JSON). This is plain HTTP, no scope involvement needed. Clicking a pin resolves it to an entity ID via `/pin/...` and passes the ref to the active view.

**View mode toggle (`App.tsx`)**: A checkbox controlling whether the selected pin is rendered via `ArticleView` (rich, typed) or `UnknownEntityView` (raw CBOR tree). Both are wrapped in `<Suspense>`. Switching modes demonstrates that the same entity can be loaded with different codecs.

### Additional dependencies

- `react`, `react-dom` (dependencies of examples package)
- `@fxtr/react` (workspace dependency)
- `vite`, `@vitejs/plugin-react` (devDependencies for dev server)

### Scripts

```json
{
  "scripts": {
    "server": "tsx src/server.ts",
    "client": "tsx src/client.ts",
    "web": "vite",
    "web:build": "vite build"
  }
}
```

Usage:
```bash
# Terminal 1: start the server
pnpm --filter @fxtr/examples server

# Terminal 2: start the web client
pnpm --filter @fxtr/examples web
# Opens browser at http://localhost:5173, connects to http://localhost:3000
```

## Component design

### `App`

Sets up the HTTP connection and provides the fxtr scope to the React tree. Manages view mode state (rich vs raw) and selected pin.

```tsx
// Created outside the component — lives for the lifetime of the page.
const ns = createHttpReadNamespace(withBaseUrl("http://localhost:3000"));

function App() {
    const [richMode, setRichMode] = useState(true);
    const [selectedRef, setSelectedRef] = useState<EntityRef | null>(null);

    return (
        <FxtrProvider scope={ns.scope}>
            <PinBrowser onSelect={setSelectedRef} />
            <label>
                <input type="checkbox" checked={richMode}
                       onChange={(e) => setRichMode(e.target.checked)} />
                Rich view (ArticleView)
            </label>
            {selectedRef && (
                <FxtrScope key={selectedRef.entityId.key()}>
                    <Suspense fallback={<div>Loading...</div>}>
                        {richMode
                            ? <ArticleView entityRef={selectedRef} />
                            : <UnknownEntityView entityRef={selectedRef.withLoadCodec(unknownLoadCodec)} />
                        }
                    </Suspense>
                </FxtrScope>
            )}
        </FxtrProvider>
    );
}
```

The `<FxtrScope key={...}>` ensures that switching pins creates a fresh scope, cleaning up the previous entity's tracked data.

### `PinBrowser`

Fetches pin list via plain HTTP. When user clicks a pin, resolves it to an entity ID (via `/pin/...` endpoint), constructs an `EntityRef<Article>` with the Article codec, and calls `onSelect`.

### `ArticleView`

Rich typed view — demonstrates `useFxtrLoad` + Suspense cascading:

```tsx
function ArticleView({ entityRef }: { entityRef: EntityRef<Article> }) {
    const article = useFxtrLoad(entityRef);
    return (
        <div>
            <h1>{article.title}</h1>
            <p>{article.body}</p>
            <Suspense fallback={<span>Loading author...</span>}>
                <AuthorBadge authorRef={article.author} />
            </Suspense>
            <Suspense fallback={<span>Loading tags...</span>}>
                <TagList tagRefs={article.tags} />
            </Suspense>
        </div>
    );
}

function AuthorBadge({ authorRef }: { authorRef: EntityRef<Author> }) {
    const author = useFxtrLoad(authorRef);
    return <span>{author.name} &lt;{author.email}&gt;</span>;
}

function TagList({ tagRefs }: { tagRefs: EntityRef<Tag>[] }) {
    return (
        <ul>
            {tagRefs.map((ref, i) => (
                <Suspense key={i} fallback={<li>...</li>}>
                    <TagBadge tagRef={ref} />
                </Suspense>
            ))}
        </ul>
    );
}
```

### `UnknownEntityView`

The CBOR tree viewer. Loads the entity via `unknownLoadCodec` (returns `UnknownEntity` with `.raw_cbor`), then renders the CBOR value as a collapsible tree.

#### CBOR tree rendering

The tree renderer is a recursive component that handles each `FxtrCbor` variant:

```tsx
function CborTree({ value, indent }: { value: FxtrCbor; indent: number }) {
    // Dispatch on value type:
    // - null, boolean, number, string → inline literal
    // - Uint8Array → hex string
    // - RawWeakFxtrID, RawStrongFxtrID, RawFxtrEntityMarkerID → <FxtrIdBadge>
    // - array → collapsible [...] with items
    // - object → collapsible {...} with key: value pairs
}
```

**Scalars** (`null`, `boolean`, `number`, `string`, `Uint8Array`): rendered inline as styled `<span>` elements, matching JSON syntax coloring. Strings are quoted, bytes are hex-encoded with a `0x` prefix.

**Objects and arrays**: rendered as collapsible blocks. Default state is expanded (matching pretty-printed JSON). Each has a toggle to collapse it to a single-line summary:

- Object collapsed: `{key1, key2, ...}` (just the keys)
- Array collapsed: `[3 items]`

Indentation is tracked via an `indent` prop (number of levels), rendered as leading spaces in a `<pre>`-style element. The whole tree sits in a monospace container so the indentation aligns.

```tsx
function CollapsibleObject({ entries, indent }: {
    entries: [string, FxtrCbor][];
    indent: number;
}) {
    const [collapsed, setCollapsed] = useState(false);

    if (collapsed) {
        const summary = entries.map(([k]) => k).join(", ");
        return (
            <span onClick={() => setCollapsed(false)} style={clickableStyle}>
                {`{${summary}}`}
            </span>
        );
    }

    return (
        <>
            <span onClick={() => setCollapsed(true)} style={clickableStyle}>{"{"}</span>
            {"\n"}
            {entries.map(([key, val], i) => (
                <Fragment key={key}>
                    {pad(indent + 1)}
                    <span style={keyStyle}>"{key}"</span>: {" "}
                    <CborTree value={val} indent={indent + 1} />
                    {i < entries.length - 1 ? "," : ""}
                    {"\n"}
                </Fragment>
            ))}
            {pad(indent)}
            {"}"}
        </>
    );
}
```

#### FxtrID badges

When a `FxtrCbor` value is a `RawWeakFxtrID`, `RawStrongFxtrID`, or `RawFxtrEntityMarkerID`, it's rendered as an `<FxtrIdBadge>`:

```tsx
function FxtrIdBadge({ rawRef, indent }: { rawRef: RawFxtrRef; indent: number }) {
    const [expanded, setExpanded] = useState(false);
    const id = rawRef.innerId;
    const tagLabel = rawRef instanceof RawStrongFxtrID ? "strong"
                   : rawRef instanceof RawFxtrEntityMarkerID ? "marker"
                   : "weak";

    if (!expanded) {
        return (
            <span onClick={() => setExpanded(true)} style={badgeStyle}>
                [{tagLabel}: {id.idString()}]
            </span>
        );
    }

    // Expanded: load the entity and show its full CBOR tree inline
    return (
        <span>
            <span onClick={() => setExpanded(false)} style={badgeStyle}>
                [{tagLabel}: {id.idString()}]
            </span>
            {" → "}
            <Suspense fallback={<span style={dimStyle}>loading...</span>}>
                <InlinedEntity entityId={id} indent={indent} />
            </Suspense>
        </span>
    );
}

function InlinedEntity({ entityId, indent }: { entityId: FxtrID; indent: number }) {
    const ref = useMemo(
        () => new EntityRef(entityId, unknownLoadCodec),
        [entityId],
    );
    const entity = useFxtrLoad(ref);
    return <CborTree value={entity.raw_cbor} indent={indent} />;
}
```

Key properties:
- **Default collapsed**: ID badges start collapsed, showing just the ID string and ref type (weak/strong/marker). No load happens until the user clicks.
- **Lazy load on expand**: clicking the badge expands it and triggers `useFxtrLoad` inside a `<Suspense>` boundary. The entity's full CBOR is fetched and rendered inline as a nested `CborTree`.
- **Recursive**: the inlined entity's CBOR may itself contain FxtrIDs, which are rendered as badges that can be expanded further. This gives you an infinite drill-down into the entity graph.
- **Suspense per badge**: each expanded badge has its own `<Suspense>` fallback, so expanding one ID doesn't block the rest of the tree.

## Implementation steps

- [x] Add `react`, `react-dom`, `@fxtr/react` as dependencies to `@fxtr/examples`
- [x] Add `vite`, `@vitejs/plugin-react` as devDependencies
- [x] Create `vite.config.ts`
- [x] Create `index.html` entry point
- [x] Implement `src/web/main.tsx` — React root
- [x] Implement `src/web/App.tsx` — layout, HTTP namespace wiring, view mode toggle
- [x] Implement `src/web/PinBrowser.tsx` — pin listing + selection
- [x] Implement `src/web/ArticleView.tsx` — rich typed view (Article, AuthorBadge, TagBadge)
- [x] Implement `src/web/UnknownEntityView.tsx` — collapsible CBOR tree with FxtrID badges
- [x] Add `web` and `web:build` scripts to package.json
- [ ] Verify end-to-end: server + web client, browse pins, view articles with authors and tags
- [ ] Verify end-to-end: raw mode, expand FxtrID badges, drill into entity graph
- [x] Update CODE_OVERVIEW.md
