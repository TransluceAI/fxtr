# Chunk 4 — the bindings file (export/import)

*(Pre-scoping record: settled constraints + the questions to answer at
chunk start. Implementation steps get written when the chunk begins.)*

Sandboxed bindings join the at-rest state: exported as a sorted,
deterministic, human-readable TOML file in the at-rest git repo (the
lockfile role), restored by import — inert until the importing
runner's sandbox repo is configured.

## Settled constraints (from `notes/sandboxed_bindings.md` + the
at-rest doctrine)

- **Ephemeral bindings stay unexported**; sandboxed bindings are the
  deliberate exception (`notes/at_rest_format.md`'s "not exported"
  section already states the split).
- One TOML file at the repo root (`bindings.toml`, provisional name);
  sorted and deterministic like every at-rest file; coords in
  dependency selections reuse the at-rest encoding conventions; the
  bound key id and pinned dependency key ids are store references
  (the extraction contract — their closures are in the exported
  entity store via the liveness obligation at bind time).
- Import follows the existing shape: parse/verify everything before
  writing (referenced ids resolvable in the shared store → the
  `MissingEntitiesError` batch; canonical-file laws), restore inside
  the one locked transaction, re-derive nothing (bindings have no
  stamps). The runner's repo path is machine-local and never
  exported.

## Open questions to settle at chunk start

1. **Exact TOML schema**: array-of-tables vs table-per-binding keyed
   how (binding key ids are store-reference strings — fine as table
   content, probably not as TOML keys); field names; how the
   dependency map nests; how the JSON config embeds (inline TOML
   value vs canonical JSON string — determinism of the round trip is
   the constraint).
2. **Verification rules**: the full pre-write check list for the new
   file (sorted/unique laws, id resolvability, coords validity
   against… nothing? — bindings may reference grids that don't exist
   in the imported layout; decide whether that's a verify error or
   the documented fault-at-connect behavior).
3. **Manifest/versioning**: whether adding the file needs a manifest
   version bump, and the compatibility stance for importing layouts
   that predate the file (presumably: absent file = zero bindings).
4. **Omit-if-empty**: apply the existing law (no bindings → no file)?
5. Doc landing: the file-schema section of `notes/at_rest_format.md`
   (the note currently points at `notes/sandboxed_bindings.md` for
   the shape; this chunk moves the schema specifics into the at-rest
   note where the other file schemas live).
