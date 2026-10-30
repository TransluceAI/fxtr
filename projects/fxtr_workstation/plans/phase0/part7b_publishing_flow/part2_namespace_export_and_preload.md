# Part 2 — namespace export (pure) + namespace read view + `entityFromTsvPin` (FOD)

> Status: RESHAPED DRAFT (2026-06-14). Design context: `design/06`
> ("Namespace exports + preload layers"), `design/01` (TSV format), `design/02`
> (writer API), DECIDED-10/15, RUNNER-2..6, OPEN-16.
>
> **Depends on part 1** (the runner-manager: repo store + rehost + the
> read-adapter shape) and the stage-14 prerequisite (repo entity store +
> the `fxtr_namespace.toml` rename).
>
> **Reshape note (2026-06-14, fifth sync).** The earlier draft had a generic,
> knobby `preload_from_remote` writer (`preloadKeys`/`extraIdStrings`/`parents`/
> `pinAll`). That is **dropped**. "Preload" is not a helper — it is a
> *capability*: **any** derivation that reads `FXTR_REMOTE_*` and pins a real
> `outputHash` is a preload (a FOD that fetches from the build-time rehost).
> Part 2 ships exactly one such helper, **`entityFromTsvPin`** (the single
> demo need); arbitrary build-time fetching / custom python builders are the
> reframed part-3 builder "roll your own FOD" path, not a part-2
> surface. Purity is likewise not enforced here — it is just "don't wire a
> remote" (a flag), which falls out of the part-3/4 reframe.

## Goal

The bridges between a git **namespace** (a `fxtr_namespace.toml` + committed
TSVs) and nix:

1. **Namespace export** (pure derivation): a namespace's committed TSVs → a
   publication manifest. Backs the new `fxtr.namespaces.<name>` flake slice.
   Server-only; no network, no layer.
2. **Namespace read view** (manager-side, RUNNER-5b): open a published
   `fxtr.namespaces.<name>` and get a real `FxtrReadNamespace` iterating that
   namespace's pins, with data resolved through the repo entity store.
3. **`entityFromTsvPin`** (FOD): the single-pin `fxtr.entities.<name>`-from-a-
   TSV-pin form (DECIDED-10). Resolves a pin key → id from the committed TSVs
   (pure), then fetches that id's bytes via the part-1 rehost into a one-pin
   layered store so *build steps* can read it.

Writer logic is testable in-process (no nix, in-process rehost over a
`RepoEntityStore`); the nix E2E rides part 1's harness.

## Moving parts

### 1. Marker-only config parse (small fxtr2 addition)

Both the export writer and the TSV-pin resolution run **inside a pure/FOD
build sandbox**, where only the flake source is present — the gitignored
`.fxtr/` dir is **not**. So they must read the namespace's `reserved_prefix`
(needed for key↔file mapping) from `fxtr_namespace.toml` **alone**, never via
`TsvNamespaceConfig.load_config` (which calls `find_fxtr_dir` + resolves the
sqlite/store-namespace-id and therefore requires `.fxtr/`).

- Add a **public, marker-only** helper to `fxtr2.data.tsv_namespace.config`
  (exposing what private `_parse_store_config` already does): given a
  namespace root, parse `fxtr_namespace.toml` and return its `reserved_prefix`
  (+ optional name/description). No `.fxtr/`, no user config, no VCS probing.
- The export/pin writers use this helper + `tsv_file.read_tsv` +
  `key_utils.{parse_pin_key,make_full_key}` — all pure file operations.

### 2. `namespace_export` writer kind (pure)

New kind in `fxtr_nix_writer` (`writers.py` + `cli.py:_WRITERS`), run via
`runFxtrWriter` — *not* a FOD, no env vars (the existing `hello_world` writer
is the template; this one writes a manifest JSON rather than a layer):

- Spec binds to a **namespace marker** (RUNNER-2 — "bind a flake attribute to
  a `fxtr_namespace.toml`"; this binding is *new work here*, the stage-14
  prereq only shipped the reusable config helpers):
  `{ kind = "namespace_export"; namespaceRoot = <store path to the dir
  containing fxtr_namespace.toml>; namespaceName = "<published name>"; }`.
  The whole namespace (its `fxtr_namespace.toml` + all `*.fxtr.tsv` under the
  root) reaches the build as flake-source paths. (No `tsvFiles` escape hatch —
  marker is the only input.)
- Read `reserved_prefix` via the §1 marker-only helper, parse + validate every
  `*.fxtr.tsv` with `tsv_file.read_tsv` (sorted keys, no dup/colon violations,
  ids parse). Build full keys with `key_utils.make_full_key`.
- Output (**OPEN-16**): `$out/fxtr_namespace_export.json` only — **no** TSV
  copy (the TSVs already live in the flake source; duplicating them here is
  pointless):
  `{ "format_version": 1, "name": "<namespaceName>",
     "pins": { "<full key>": "<id_string>" } }`.
  Full keys **keep their `prefix:` part** so the read view's `list_pins(prefix)`
  works directly off the manifest. Build-time validation: every id parses as a
  `FxtrID` (cheap, catches typos).

### 3. `lib.fxtr.tsvNamespaceExport`

`nix/lib/tsvNamespaceExport.nix` (renamed from the draft's `gitNamespaceExport`
— this exports a *tsv namespace*; the "git" naming was a leftover the
overview's rename note explicitly retired):

```nix
tsvNamespaceExport { namespaceRoot, name, system ? ... }
  -> { type = "fxtr_namespace_export"; export = <drv>; }
```

A thin `runFxtrWriter` wrapper (cf. how `mkEntityFromPython` wraps its writer).
Declared in user flakes as `fxtr.namespaces.<name>`. Keeps part5's invariants:
`export` is the buildable leaf; `type` is cheap eval metadata.

### 4. Namespace read view (manager-side, RUNNER-5b)

The consumer half of (2)+(3), living in the part-1 **manager** tier
(`fxtr_nix_runner`). A **real `FxtrReadNamespace` subclass** (unlike part 1's
entities adapter, which stayed "shaped" only because it *can't* do an
id-valued `list_pins` without a build — here the manifest *is* the key→id map,
so the full ABC is satisfiable). It mirrors `HttpFxtrReadNamespace`
(`fxtr2/experimental/http/client.py:305`) with two swaps: pins come from the
in-memory manifest instead of `GET /pins`, and the deep-fetch source is the
**repo store** instead of an http client.

`mgr.namespace(span, attr)` (working name — takes a `BuildSpan` like
`mgr.entities(span)`, since the export must be *built*):

- eval `fxtr.namespaces.<name>.type` (validate shape) → build `.export` →
  `materialize` → read `fxtr_namespace_export.json` **once** at construction.
- On enter, open an internal `FxtrScope` and
  `register_deep_fetch_source(repo_store, IndependentLocalBridge(lease=...))`
  on **its own** scope (the `mgr.repo_store` `(DeepFetchable, Lease)` pair;
  `Independent` because the store session outlives the view — part 1 §3
  lifetime invariant). The caller's scope is never touched except by an
  explicit `retrieve(into_scope=...)`.
- `list_pins(prefix)` → the manifest's `pins` map (filtered by prefix).
- `_retrieve_impl(key, into_scope, ...)` → `id = manifest[key]`; attach the id
  into the view's own scope via the store source; resolve into `into_scope`
  via `copy_from_scope`/`lift` (exactly `HttpFxtrReadNamespace._retrieve_impl`).
  No layer is involved — the export is metadata; the store supplies bytes
  (DECIDED-15). **Side-effect-free** w.r.t. the caller scope beyond the one
  attached entity.
- This is a *separate* read namespace from part 1's entities adapter
  (RUNNER-5a): you open it per `fxtr.namespaces.<name>` and it gives you a
  whole namespace object; the entities adapter is one namespace whose pins
  are the `fxtr.entities.*` attr names.

### 5. `entityFromTsvPin` writer kind (FOD)

New kind in `fxtr_nix_writer` — a new case alongside `hello_world` /
`mkEntityFromPython`, **not** a generic preload helper:

- Spec: `{ kind = "entity_from_tsv_pin"; namespaceRoot = <...>; key = "<full
  key>"; }`. (J2: only `(namespaceRoot, key)` — no direct-id input. "Fetch an
  arbitrary id" is the part-3/4 roll-your-own-FOD path.)
- Behavior, at build time (only the byte-fetch is impure):
  1. Resolve `key → id` purely from the committed TSVs (§1 marker-only parse +
     `tsv_file.read_tsv` + `key_utils`).
  2. Build a remote from the env: `FXTR_REMOTE_BASE_URL` +
     `FXTR_REMOTE_BEARER_TOKEN` → an `httpx.AsyncClient` (base_url + a
     `Authorization: Bearer <token>` header) wrapped in
     `HttpDeepFetchable(client, UnconditionalLocalLifetime(...))`, exposed as a
     `(DeepFetchable, Lease)` pair. **This `(base_url, token) → remote` adapter
     does not exist yet** — the pattern is inline in
     `HttpFxtrReadNamespace.__aenter__`; factor it into a small reusable
     "remote from env" helper (home: `fxtr_nix_writer`, or a tiny fxtr2 helper
     if cleaner) shared by this writer.
  3. `async with LayeredFileStoreIsolatedWriter(out, parents=[], remote=...)`:
     `mark_as_remote(id)` then `preload(id)` then `pin_to_this_layer(key, id)`.
     Closure bytes land in `data/`; anything left remote keeps a precomp
     marker. `validate_on_close=True`.
- The derivation declares `impureEnvVars = [ FXTR_REMOTE_BASE_URL,
  FXTR_REMOTE_BEARER_TOKEN ]` and `outputHash` (recursive sha256). v0
  workflow: `lib.fakeHash` → copy the real hash from the mismatch error (the
  runner-owned relock tool lands in **part 2b**).
- **Determinism**: layer output must be byte-stable for a fixed input id —
  `pins.json`/`parents.json` writes sorted; data files canonical CBOR; precomp
  JSON ordering **[TODO verify json key order + no timestamp/nondeterminism
  leak; the build-twice byte-diff test is the gate]**.

### 6. `lib.fxtr.entityFromTsvPin`

`nix/lib/entityFromTsvPin.nix` — sugar over the §5 writer, the git-pin-sourced
`fxtr.entities.<name>` form from DECIDED-10:

```nix
entityFromTsvPin { namespaceRoot, key, outputHash, system ? ..., name ? ... }
  -> { type = "fxtr_layer_entity"; layer = <FOD layer drv>; layerKey = key; }
```

Drops straight in as a normal `fxtr.entities.<name>` (usable anywhere layer
entities are). `outputHash` stays the low-level arg; part 2b's lock tool will
own re-pinning it.

## Implementation steps

- [x] Marker-only config helper in `fxtr2.data.tsv_namespace.config` + unit
      test (parses `reserved_prefix` from a marker with no `.fxtr/` present).
      *(Done: `load_namespace_marker` / `TsvNamespaceMarker` in `config.py`;
      tests in `tests/test_tsv_namespace/test_config.py::TestLoadNamespaceMarker`.)*
- [x] `namespace_export` writer + unit tests (pure python, tmpdir namespace:
      happy path, unsorted/dup/colon failures, full-key prefix preserved).
      *(Done: `write_namespace_export` in `nix/fxtr_nix_writer/.../writers.py`
      (registered in `cli.py`); tests in
      `nix/py_testing/tests/test_namespace_export.py`.)*
- [x] `tsvNamespaceExport.nix` + wiring in `nix/lib/default.nix` + a repo-root
      flake check target + py_testing test reading the manifest back.
      *(Done: `nix/lib/tsvNamespaceExport.nix`; `packages.x86_64-linux.namespace-export-check`
      in `flake.nix`; nix round-trip test in the same test file. Verified the
      build end-to-end via a `path:` flake ref — offloads to the linux builder
      and emits the expected manifest.)*
- [x] Namespace read view (§4) in the manager + tests: build+read a manifest,
      `list_pins`, `retrieve` a pin's bytes through an in-process
      `RepoEntityStore`; assert the caller scope only gains the requested id.
      *(Done: `namespaces.py` — `NamespaceReadAdapter` (a real `FxtrReadNamespace`)
      + `build_namespace_manifest`; `mgr.namespace(span, name)` composes them.
      Tests in `projects/fxtr_nix_runner/tests/test_namespaces.py`. Mirrors
      `HttpFxtrReadNamespace` with the repo store via `store.try_lookup` as the
      source.)*
- [x] `(base_url, token) → (HttpDeepFetchable, Lease)` "remote from env"
      helper + unit test.
      *(Done: `http_deep_fetchable` in `fxtr2.experimental.http.client`;
      tested against `rehost_context` in `fxtr_nix_runner/tests/test_rehost.py`
      (round-trip + 401-without-token).)*
- [x] `entity_from_tsv_pin` writer + in-process tests (no nix): writer fn
      against part 1's `rehost_context` over a `RepoEntityStore`; assert layer
      contents + reader round-trip + purity (a pure reader with no remote can
      load the preloaded pin).
      *(Done: `write_entity_from_tsv_pin` (env path) + `write_preload_layer`
      (core) in `writers.py`; core tested in-process against a direct
      `RepoEntityStore` remote in `nix/py_testing/tests/test_entity_from_tsv_pin.py`
      — pin recorded, bytes local, pure-reader round-trip, missing-id raises. The
      full env→http path rides the nix E2E.)*
- [x] Determinism test (build the writer output twice in-process; byte-diff).
      *(Done: `test_preload_layer_is_byte_stable` — same dir tree, byte-identical.)*
- [x] `entityFromTsvPin.nix` + wiring; nix E2E via part 1's harness (the v0
      `fakeHash` → parse-mismatch → re-pin recipe). **Also the full
      remote-pointer entities-adapter E2E** (deferred from part 1).
      *(Done: `entityFromTsvPin.nix` + the `runFxtrWriter` FOD extension
      (outputHash/impureEnvVars + cacert so the writer's httpx builds an SSL
      context in-sandbox). `test_entity_from_tsv_pin_remote_pointer` in
      `test_nix_e2e.py` over a new `_workdir_templates/tsv_pin_entity` fixture:
      a parent→leaf entity pinned via `entityFromTsvPin`, built through the
      manager (fakeHash → re-pin the live workdir), read back via
      `mgr.entities(span)` with the remote leaf resolved through the repo store.
      **Passes on podman**; skips on the darwin host backend.)*
- [x] Update `nix/CODE_OVERVIEW.md` (+ fxtr_nix_writer docstrings),
      `projects/fxtr_nix_runner/CODE_OVERVIEW.md` (the namespace read view),
      and `v2/.../tsv_namespace/LOCAL_CODE_OVERVIEW.md` (the marker-only helper).

## Discuss / TODOs

- **Layer-output determinism** (§5): the FOD hash only works if the writer
  output is byte-stable for a fixed input id — verify precomp-JSON key ordering
  and that nothing nondeterministic leaks; any offender is an fxtr2 fix. The
  build-twice test is the gate.
- **OPEN-16**: manifest format final call. Resolved leanings now inlined in §2
  (JSON-only, full keys keep `prefix:`, ids validated at build time). Remaining:
  whether the export also validates that a *referenced* key resolves (n/a now
  that there's no `preloadKeys`).
- **Where in-process writer tests live**: `nix/py_testing/tests/` (has
  fixtures). Decided.
- **`mgr.namespace` signature**: confirm `(span, attr)` once the read view is
  coded — it needs a span to build `.export`, but per-`retrieve` resolution is
  pure store lookups (no rehost), so the span is only used at construction.
