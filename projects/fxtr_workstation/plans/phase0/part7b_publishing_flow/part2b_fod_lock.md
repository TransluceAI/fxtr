# Part 2b — FOD lock file + runner-owned relock tool

> Status: IMPLEMENTED (2026-06-15; was `part5b_fod_lock.md`, pulled forward next
> to parts 1/2). Design decisions: (1) **decouple** the lock reader from the FOD
> helpers; (2) `update-hashes` is **incremental by default** (locks new slots,
> drops stale ones; never auto-relocks an already-locked slot) with `--all` /
> `-k` for explicit re-locking. (We explored a committed `drvPath` staleness
> fingerprint to auto-detect in-place input edits, but dropped it — it forced a
> noisy committed fingerprint, made FOD-source narrowing correctness-critical,
> and over-triggered on nixpkgs bumps; explicit `--all`/`-k` is the simpler
> contract.)
> Depends on parts 1–2 (the manager: repo store + rehost; the `entityFromTsvPin`
> FOD). Now that `fxtr_nix_runner` owns the repo store and rehost
> (RUNNER-2/3/7), the relock tool is **fully self-contained in the runner** —
> no external publishing orchestration.

## Goal

Replace the manual `lib.fakeHash` → copy-mismatch-error dance with two
**independent** pieces:

1. A **lock file** (`fod_lock.toml`) plus a small, self-contained nix
   **lock-reader** the user drops in wherever they would otherwise have written
   `lib.fakeHash`. The reader is *not* coupled to any FOD helper — it just turns
   a key into a pinned `outputHash` by reading the lock file.
2. A **relock command** (`fxtr_nix update-hashes`) that keeps the lock in sync
   with the declared `fxtr.preloads` slots: it builds newly-declared slots,
   records their fixed-output hashes, and drops slots no longer declared.
   In-place input changes under an existing key are re-locked explicitly
   (`--all` / `-k`), not auto-detected.

## Headline 1: the reader is decoupled from the helpers

`outputHash` is already the single universal seam on every FOD (`runFxtrWriter`
takes it; `entityFromTsvPin` forwards it; the part-3 builders will
too). So the lock file is read by a **standalone reader function**, never by the
helpers themselves:

```nix
# configured once at the top of the flake (lockFile path lives here)
fodLock = lib.fxtr.fodLockReader { lockFile = ./fod_lock.toml; };

# dropped in wherever lib.fakeHash would have gone — any FOD, any helper
fxtr.entities.thePin = lib.entityFromTsvPin {
  namespaceRoot = ./.;
  key = "thePin";
  outputHash = fodLock "thePin";     # <- reads the pinned hash from the lock
};
```

No `lockKey`/`lockFile` args on any FOD helper; no "lock-file path plumbing".
The reader is helper-agnostic — its only integration point is "produce an
`outputHash`". `lib.fakeHash` stays available as the raw escape hatch.

### Reader behavior (`fodLockReader`)

- `lib.fxtr.fodLockReader { lockFile }` → a **bare function** `key -> sri-hash`.
- Implementation: `let lock = builtins.fromTOML (builtins.readFile lockFile);`
  then `key: lock.hashes.${key} or (throw "...")`. `builtins.fromTOML` reads
  quoted keys (e.g. `"data/models:encoder"`) fine.
- **Missing-key behavior**: `throw` naming the key + the fix command
  (`fxtr_nix update-hashes`). Pure eval never mints sentinels — the tool writes
  them during an update pass (below), sidestepping "base64 arbitrary bytes in
  nix".

## Headline 2: `update-hashes` is incremental by key set, not by content

The default pass syncs the lock to the *set* of declared `fxtr.preloads` slots:

- **Lock** slots that are new (declared but absent from the lock) — sentinel +
  build + record the real hash.
- **Drop** slots that are stale (in the lock but no longer declared).
- **Leave** already-locked slots untouched — no eval, no build.

It deliberately does **not** try to detect that an existing slot's *inputs*
changed in place (e.g. a TSV re-pointed at a different id under the same key).
That's an explicit action: `--all` re-locks every slot, `-k/--key` a chosen
subset. (Rationale in the status note: a `drvPath` staleness fingerprint would
auto-detect this but at real cost — a committed/noisy fingerprint, FOD-source
narrowing promoted to a correctness requirement, and spurious relock-all on
every nixpkgs bump. The explicit contract is simpler and predictable.)

To verify a slot still reproduces its locked content, a user runs `nix build
--check <target>` themselves; we don't ship a rebuild-everything mode.

**Note on FOD source hygiene:** `entityFromTsvPin` narrows its source with
`builtins.path` to the marker + `*.fxtr.tsv` (excluding `flake.nix`,
`fod_lock.toml`, VCS files). This is good practice — precise inputs — and keeps
the produced layer independent of the lock file it's pinned by. It is no longer
*correctness*-critical (there's no `drvPath` fingerprint to keep stable), so
broader narrowing for other FODs is a pure-efficiency follow-up (`followups.md`).

## The lock file: `fod_lock.toml`

TOML, pretty-printed, sorted, at the flake root — a single `[hashes]` table,
exactly what `fodLockReader` reads back (`hashes.<key>` is the SRI string):

```toml
format_version = 1

[hashes]
greeting = "sha256-…"
"data/models:encoder" = "sha256-…"
```

- TOML by preference: human-readable/diffable; `builtins.fromTOML` reads it.
  Special keys (`:` / `/`) are written quoted.
- Written with `tomlkit` (declared explicitly in `fxtr_nix_runner/pyproject.toml`),
  sorted by key. The committed lock holds only the hashes the flake needs — no
  tool-internal metadata.

## Sentinel hashes (the error→slot mapping)

Unchanged. Any 32 bytes is a valid sha256, so the *tool* writes a distinct
**sentinel** per slot being relocked — the slot key encoded/padded into the hash
bytes, SRI-encoded. Each `hash mismatch` error's `specified:` hash then uniquely
identifies its slot; no nix-source parsing, no name-mapping. Sentinels are
written **only for the slots being relocked**; untouched slots keep their real
hashes so they build from cache.

## Target discovery — explicit `fxtr.preloads`

Flakes list hash-locked slots under **`fxtr.preloads.<lockKey> = <drv>;`** (the
buildable FOD leaf, e.g. `(lib.entityFromTsvPin {…}).layer`, or a user
part-3 builder FOD). `update-hashes` does one cheap `nix eval --apply
builtins.attrNames` over `fxtr.preloads` to get the slot keys (values not
forced, so the reader never throws on a missing/incomplete lock here), and
builds `<ref>#fxtr.preloads.<key>` for the slots it relocks.

The key necessarily appears twice — `fxtr.preloads.<key>` (for the tool to find
the drv) and `fodLock "<key>"` (for eval to read the hash). These cannot be
unified (`preloads` needs the built drv; the reader runs before it exists); a
user can `let k = "thePin"` to DRY. No wrapper to tie them.

Implicit attrset-walking / input-drv-closure discovery was considered and
dropped for v0 (preload layers that are only *parents* of pure layers aren't
reachable by walking `fxtr.*`, and implicit discovery is harder to reason about).

## The relock engine — `fxtr_nix update-hashes`

Home: `fxtr_nix_runner` (manager tier). Self-contained — opens the repo store +
rehost itself, no caller-supplied env.

### Modes (pure `plan_relock`, no nix)

- **default** (`fxtr_nix update-hashes [PATH]`): relock = preload keys ∖ lock
  keys (new); drop = lock keys ∖ preload keys (stale); leave the rest.
- **`--all`**: relock every `fxtr.preloads` key (also drops stale). The
  "I changed inputs, re-verify everything" hammer.
- **explicit keys** (`-k/--key KEY`, repeatable): relock exactly those (must be
  in `fxtr.preloads`; error otherwise). `--all` and `-k` are mutually exclusive.
- **`--dry-run`**: print the plan (would-relock new / would-drop stale) and
  write nothing; exit nonzero if anything would change. The cheap CI check that
  the lock's key set matches the declared slots (it does *not* detect in-place
  content drift — that's `nix build --check`).

### The relock loop (over the computed target set)

1. Open the repo's `RepoEntityStore` (create-if-absent) + stand up the rehost
   (part 1's `rehost_context`) inside a `FlakeManager` build span. Reference the
   flake by its git ref (live workdir, `rev=None`).
2. `nix eval --apply builtins.attrNames` over `fxtr.preloads` → slot keys;
   compute the target set per mode; apply drops to the in-memory lock.
3. Write sentinels into the lock for the targeted slots, and **stage the lock**
   if it isn't tracked yet (see note below) so the build sees it.
4. `nix build --keep-going <targets>` with the rehost env (the FODs are
   independent → all mismatch in one round).
5. Parse the `hash mismatch` messages from stderr; map each `specified:`
   sentinel → slot key; write each `got:` hash into `[hashes]`.
6. Repeat until no slot remains (usually one round). Any *non*-mismatch failure
   aborts with the real error.
7. Write `fod_lock.toml` back (sorted, pretty).

**Lock visibility (git semantics).** The flake is read at the live workdir, and
nix's git fetcher only sees git-*tracked* files — so a freshly-created
`fod_lock.toml` must be tracked or the build fails "path not tracked by Git".
The tool stages it (`git add`, mirroring how nix auto-stages `flake.lock`); once
tracked, later in-place rewrites are seen without re-staging. **Podman caveat:**
host-side `git add` of a *new* file writes an index the builder's in-container
libgit2 rejects (version skew), so on the podman backend the lock must already
be tracked (commit an empty `fod_lock.toml` first; the part-6 example scaffolds
this). The host backend has no such issue. (See `followups.md`.)

Decided (carried from part 5b): hash updating is **not** folded into
`fxtr_nix lock` / `nix flake lock` — locking is pure input metadata;
update-hashes needs a live rehost + runs builds, so conflating them would make
`lock` sometimes-slow and sometimes-need-a-server.

### CLI surface

Mirror the existing `lock [PATH]` positional convention (`__main__.py`):

```
fxtr_nix update-hashes [PATH] [--all] [--dry-run] [-k/--key KEY]...
```

`PATH` positional (default cwd); keys via repeated `-k` (avoids colliding with
the path positional). Argparse wiring next to the existing `lock` subparser; a
`_update_hashes(...)` driver alongside `_lock`.

## Implementation steps

- [x] `lib.fxtr.fodLockReader { lockFile } -> (key -> sri)` in
      `nix/lib/fodLockReader.nix` + wiring in `nix/lib/default.nix`; the
      missing-key `throw`. Lazy read (applying to `{ lockFile }` doesn't touch
      the file) so `attrNames` over a FOD attrset never forces a hash.
- [x] Narrow `entityFromTsvPin`'s source (`builtins.path`, marker + `*.fxtr.tsv`,
      excluding `fod_lock.toml`) — source hygiene (precise inputs); keeps the
      layer independent of the lock file.
- [x] Lock-file module `_fod_lock.py`: read/write `fod_lock.toml` (`tomllib`
      read, `tomlkit` write, sorted) — a single `[hashes]` table +
      `format_version`; `tomlkit` declared in `pyproject.toml`. Unit tests
      (round-trip, special keys, missing-file → empty lock).
- [x] Sentinel mint + mismatch parse helpers (`_fod_lock.py`) + unit tests
      (deterministic/distinct/valid-SRI sentinels; multi-block + label-order
      mismatch parsing). *Parses the human `nix build` stderr (proven by the
      existing E2E); a fixture-style unit test pins the message shape so an
      upstream change fails loudly.*
- [x] Relock engine `update_hashes.py` (manager tier): `attrNames` discovery +
      pure `plan_relock` (default = new + drop-stale / `--all` / `-k`) +
      sentinel-write + git-stage-if-untracked + `--keep-going` build + parse
      loop + `--dry-run`. Unit tests on the pure `plan_relock`.
- [x] `fxtr_nix update-hashes` CLI subcommand (argparse wiring; lazy-imports the
      fxtr2-bearing engine so other subcommands stay fxtr2-free).
- [x] E2E (nix-marked, part-1 harness): empty lock → default locks the new slot
      → read back through `mgr.entities`; re-run is a default no-op; repoint the
      TSV pin → default still leaves the locked slot, `-k thePin` re-locks it to
      the new content's hash. (Add-second-slot / drop-stale covered by the
      `plan_relock` unit tests.)
- [x] Migrate part-2's test-side two-pass `fakeHash` hack (the `tsv_pin_entity`
      fixture) to use `fodLockReader` + `fxtr.preloads` + the tool.
- [x] Update `nix/CODE_OVERVIEW.md` + `projects/fxtr_nix_runner/CODE_OVERVIEW.md`.

## Discuss / TODOs

- ~~Lock-file path plumbing~~ RESOLVED: standalone `fodLockReader { lockFile }`;
  helpers untouched.
- ~~Engine home + CLI surface~~ DECIDED: `fxtr_nix update-hashes` in
  `fxtr_nix_runner`, self-contained.
- ~~Target discovery~~ DECIDED: explicit `fxtr.preloads.<lockKey>`.
- ~~`drvPath` staleness fingerprint~~ EXPLORED + DROPPED: it would auto-detect
  in-place input edits, but forced a noisy committed fingerprint
  (nixpkgs-coupled `/nix/store` paths, relock-all churn on a nixpkgs bump) and
  promoted FOD-source narrowing to a correctness requirement. Replaced by the
  explicit `--all`/`-k` contract; in-place content verification is the user's
  `nix build --check <target>`.
- `hash mismatch` log stability: the message shape is parsed from `nix build`'s
  human stderr — a `_fod_lock` unit-test fixture pins it so an upstream change
  fails loudly. (Revisit `--log-format internal-json` if the human format
  proves unstable.)
- **Podman bootstrap from an untracked lock**: host `git add` of a new file
  isn't readable by the builder's in-container libgit2, so first-run-from-scratch
  on the podman backend needs the lock committed first. Mirrors how nix stages
  `flake.lock` in-container; a real fix would stage via the build context.
  Documented in `followups.md`.
