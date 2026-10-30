# Stage 4: TypeScript Package Split

## Goal

Split the monolithic `fxtr2` TypeScript package into a pnpm workspace monorepo with scoped packages:

- **`@fxtr/core`** — entities, refs, scopes, bicodec, system entities, arenas, namespaces. Minimal deps (`cbor2`), environment agnostic.
- **`@fxtr/http-client`** — HTTP client (HttpReadableArena, HttpDeepFetchable, HttpFxtrReadNamespace). Deps: `@fxtr/core`, `cbor2`.
- **`@fxtr/http-server`** — Hono HTTP server (createFxtrApp). Deps: `@fxtr/core`, `cbor2`, `hono`.

The workspace should be easy to extend with new packages (e.g. `@fxtr/react`, `@fxtr/react-server`).

## Current state

Single package `fxtr2` at `v2/ts/` with this source layout:

```
src/
├── core/          → entityCore.ts, visitor.ts
├── arena/         → types.ts, memoryArena.ts
├── scope/         → entityRef.ts, scope.ts, unionLoadCodec.ts, unsafeSymbols.ts
├── common/        → bicodec.ts, pyCompatSet.ts, systemEntities.ts
├── namespace/     → readNamespace.ts
├── http/          → server.ts, client.ts
└── index.ts       (empty)
```

Dependency graph (clean, acyclic):
```
core → arena → scope → common/namespace → http
```

External deps: `cbor2` (used by core, arena, http), `hono` (http/server only).

## Package boundaries

### `@fxtr/core`

Everything except HTTP. This is the full entity system:

```
packages/fxtr/src/
├── index.ts              # Public API exports
├── internal.ts           # @fxtr/core/internal entrypoint (UNSAFE_ATTACH_FROM_EXTERNAL_ARENA)
├── core/
│   ├── entityCore.ts
│   └── visitor.ts
├── arena/
│   ├── types.ts
│   └── memoryArena.ts
├── scope/
│   ├── entityRef.ts
│   ├── scope.ts
│   ├── unionLoadCodec.ts
│   └── unsafeSymbols.ts
├── common/
│   ├── bicodec.ts
│   ├── pyCompatSet.ts
│   └── systemEntities.ts
└── namespace/
    └── readNamespace.ts
```

Runtime deps: `cbor2`

**Exports** (`@fxtr/core`): Public types and functions from core, scope, common, namespace. Arena types are *not* in the public API — end users interact with scopes, not arenas directly.

**Exports** (`@fxtr/core/internal`): Arena types (`ReadableArena`, `ReadWriteArena`, `CopyTargetArena`, `DeepFetchable`, `DepKind`, `FxtrEntityFlatData`, `MemoryStore`, `MemoryArena`) and `UNSAFE_ATTACH_FROM_EXTERNAL_ARENA` symbol. Separate entrypoint so it's opt-in — the HTTP packages need arenas, but normal consumers shouldn't. Implemented via `package.json` `exports` field:

```json
{
  "exports": {
    ".": { "import": "./dist/index.js", "types": "./dist/index.d.ts" },
    "./internal": { "import": "./dist/internal.js", "types": "./dist/internal.d.ts" },
    "./bicodec": { "import": "./dist/bicodec.js", "types": "./dist/bicodec.d.ts" }
  }
}
```

**Exports** (`@fxtr/core/bicodec`): The `bicodec` module as a dedicated entrypoint so consumers can do `import * as bicodec from "@fxtr/core/bicodec"`. The field codec names (`string`, `number`, etc.) are too generic for the top-level namespace.

### `@fxtr/http-client`

```
packages/fxtr-http-client/src/
├── index.ts         # Re-exports client.ts public API
└── client.ts        # HttpReadableArena, HttpDeepFetchable, HttpFxtrReadNamespace, withHttpReadNamespace, withBaseUrl
```

Runtime deps: `@fxtr/core` (workspace:*), `cbor2`

Imports from `@fxtr/core` (public API) and `@fxtr/core/internal` (for `UNSAFE_ATTACH_FROM_EXTERNAL_ARENA`).

### `@fxtr/http-server`

```
packages/fxtr-http-server/src/
├── index.ts         # Re-exports server.ts public API
└── server.ts        # createFxtrApp, computePrecompClosure
```

Runtime deps: `@fxtr/core` (workspace:*), `cbor2`, `hono`

## Workspace layout

```
v2/ts/
├── pnpm-workspace.yaml
├── package.json              # Root: devDeps only (typescript, vitest, biome, @types/node)
├── tsconfig.base.json        # Shared compiler options
├── tsconfig.json             # Root project references (for tsc --build)
├── biome.json                # Shared lint/format config (unchanged)
├── vitest.config.ts          # Root test config, discovers all packages
├── CODE_OVERVIEW.md          # Updated to reflect new structure
└── packages/
    ├── fxtr/
    │   ├── package.json
    │   ├── tsconfig.json     # extends ../../tsconfig.base.json, references: []
    │   ├── src/              # (moved from ts/src/ minus http/)
    │   └── tests/            # (moved from ts/tests/ minus http/)
    ├── fxtr-http-client/
    │   ├── package.json
    │   ├── tsconfig.json     # references: [../fxtr]
    │   ├── src/
    │   └── tests/
    └── fxtr-http-server/
        ├── package.json
        ├── tsconfig.json     # references: [../fxtr]
        ├── src/
        └── tests/
```

## Design decisions

### Build: TypeScript project references

Each package has its own `tsconfig.json` extending a shared `tsconfig.base.json`. Packages declare `references` to their dependencies. `tsc --build` from the root compiles everything in dependency order with incremental builds.

This is the standard TS monorepo approach — no extra build tools needed, works well with declaration generation, and IDE support is solid.

`tsconfig.base.json` carries the shared compiler options (the current `compilerOptions` minus `outDir`/`rootDir`/`composite` which are per-package):

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "bundler",
    "lib": ["ES2022", "DOM"],
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "forceConsistentCasingInFileNames": true,
    "declaration": true,
    "declarationMap": true,
    "sourceMap": true,
    "resolveJsonModule": true,
    "isolatedModules": true,
    "noUncheckedIndexedAccess": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "exactOptionalPropertyTypes": true,
    "composite": true
  }
}
```

Each package's `tsconfig.json`:

```json
{
  "extends": "../../tsconfig.base.json",
  "compilerOptions": {
    "outDir": "dist",
    "rootDir": "."
  },
  "include": ["src/**/*.ts", "tests/**/*.ts"],
  "exclude": ["node_modules", "dist"],
  "references": []
}
```

(`references` filled in per package — fxtr has none, http-client and http-server reference fxtr.)

### Tests: single root vitest config

One `vitest.config.ts` at the workspace root discovers all tests:

```typescript
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["packages/*/tests/**/*.test.ts"],
  },
});
```

`pnpm test` from the root runs everything. Can still scope: `pnpm vitest run --dir packages/fxtr/tests`.

### Root package.json

Becomes a workspace root — no `name`/`version`, just devDeps and scripts:

```json
{
  "private": true,
  "type": "module",
  "scripts": {
    "build": "tsc --build",
    "check": "tsc --build --noEmit",
    "test": "vitest run",
    "test:watch": "vitest",
    "lint": "biome check packages/",
    "lint:fix": "biome check --write packages/",
    "format": "biome format --write packages/"
  },
  "devDependencies": {
    "@biomejs/biome": "^1.9.0",
    "@types/node": "^25.5.0",
    "typescript": "^5.7.0",
    "vitest": "^3.0.0"
  }
}
```

### pnpm-workspace.yaml

```yaml
packages:
  - "packages/*"
```

### Cross-package imports in tests

The HTTP client tests import from both `@fxtr/core` and `@fxtr/http-server` (to spin up a test server). `@fxtr/http-server` is a devDep of `@fxtr/http-client` for this reason.

The server tests import from `@fxtr/core` only.

### Import path rewriting

Current imports use relative paths like `../../src/core/entityCore.js`. After the split:

- Within `@fxtr/core`: relative paths stay (e.g., `../core/entityCore.js`)
- From `@fxtr/http-client` → `@fxtr/core`: `import { FxtrID } from "@fxtr/core"`
- From `@fxtr/http-client` → `@fxtr/core/internal`: `import { UNSAFE_ATTACH_FROM_EXTERNAL_ARENA } from "@fxtr/core/internal"`
- Test imports: same package-relative for same-package, scoped for cross-package

### What `@fxtr/core` exports

The current `index.ts` is empty. We need to build it out. A reasonable starting point — export everything that external packages currently import. At minimum:

**`@fxtr/core` (public API — `index.ts`):**

From `core/`: `FxtrID`, `FxtrCbor`, `RawWeakFxtrID`, `RawStrongFxtrID`, `RawFxtrEntityMarkerID`, `RawFxtrRef`, `fxtrCborDumps`, `fxtrCborLoads`, `deepFreeze`, `b32enc`, `b32dec`, error types, visitor functions.

From `scope/`: `FxtrScope`, `withFxtrScope`, `GLOBAL_SCOPE`, `EntityRef`, `Entity`, `EntityLoadCodec`, `EntityStoreCodec`, `FXTR_CODEC`, `FXTR_CACHEABLE`, `UnknownEntity`, `unknownLoadCodec`, `unionLoadCodec`, `cachedSupportsEntityType`.

From `common/`: `bicodec` namespace (or individual field codec exports), `PyCompatSet`, key functions, `FxtrBasicType`, `defineType`, `trackBuiltinTypeId`.

From `namespace/`: `FxtrReadNamespace`, `RetrieveOpts`.

**`@fxtr/core/internal` (`internal.ts`):**

From `arena/`: `ReadableArena`, `ReadWriteArena`, `CopyTargetArena`, `DeepFetchable`, `DepKind`, `FxtrEntityFlatData`, `MemoryStore`, `MemoryArena`.

From `scope/`: `UNSAFE_ATTACH_FROM_EXTERNAL_ARENA`.

This can be refined as we go — start with what the HTTP packages need, then expand.

## Implementation steps

- [x] 1. Create workspace skeleton: `pnpm-workspace.yaml`, `tsconfig.base.json`, root `tsconfig.json` with project references
- [x] 2. Create `packages/fxtr/` — move source files, set up `package.json`, `tsconfig.json`, write `index.ts` and `internal.ts` exports
- [x] 3. Create `packages/fxtr-http-server/` — move `server.ts`, set up package config, rewrite imports
- [x] 4. Create `packages/fxtr-http-client/` — move `client.ts`, set up package config, rewrite imports (including `@fxtr/core/internal`)
- [x] 5. Move tests to their respective packages, rewrite test imports
- [x] 6. Update root `package.json` (devDeps only, workspace scripts), `vitest.config.ts`, `biome.json` paths
- [x] 7. `pnpm install` to link workspace packages, verify `tsc --build` succeeds
- [x] 8. Run full test suite, fix any breakage
- [x] 9. Clean up: remove old `src/`, old `tests/`, old non-root configs
- [x] 10. Update CODE_OVERVIEW.md
