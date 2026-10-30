# 06 — No schema system for now

Decision: **don't build a schema system in phase 0.** TypeScript
type annotations at the consumer level + runtime detection of stub
class instances is enough for the core walks (refcounting,
preserve-set, dispatch). Anything fancier we can layer later.

## What the runtime actually needs

The session reducer's structured state is just:

```
StateValue =
  | null | boolean | number | string | bytes
  | StateValue[]
  | { [key: string]: StateValue }
  | EntityRefStub | ServiceRefStub | OpResultStub | CallbackInboxStub
```

The walks (pin diff, preserve diff, projection) only need to
distinguish "is this a stub instance" vs. "is this a container."
Stubs are class instances and identifiable at runtime via
`instanceof`. No schema info needed.

## What dispatch needs

Dispatch keys off a **type ID string** carried by the loaded
entity itself. We assume entities embed a small "type id" field
in their CBOR / wire form (similar to fxtr's
`system_entities.define_type` mechanism). The dispatch registry
maps `type_id_string → React component`. Walking a loaded value
to render its sub-stubs uses runtime `instanceof` checks on
stubs.

## What user code declares

Just TypeScript types, along with a _dispatch-level_ runtime type
ID verification check.

```ts
type DemoNote = {
  title: string;
  body: string;
  related: EntityRefStub<DemoNote>;
};

function isDemoNote(type_id: EntityRefStub<unknown>): bool;
```

The decoder is generic: it walks the list-escape JSON / CBOR and produces a
plain JS value with stub instances substituted in for tagged refs.
TS types are erased at runtime; if the wire value doesn't match
what user code expects, that's a runtime cast bug — same as any
unsafely typed JSON parse.

## Why this is fine

- Typescript has more powerful type inference behavior than Python;
  we can directly and idiomatically encode more complex data shapes
  without needing runtime-facing type conversion.
- The frontend mostly *reads*. Encoding parity isn't needed.
- The wire encoding (list-escape JSON for workstation, CBOR for
  HTTP) already preserves the structure. The decoder just needs
  to substitute stubs where it sees tagged refs.
- Dispatch can find sub-stubs without knowing the schema (just
  walk and check `instanceof`).
- Type IDs come from the `$fxtr.type` field, not from a user-supplied
  schema.

## What we're giving up

- **Runtime validation:** if the wire delivers something that
  doesn't match the user's TS type, we won't catch it until a
  consumer trips over it. Acceptable: the server is the source
  of truth, and we trust it.
- **Schema-driven UI generation:** can't auto-render a form based
  on schema metadata. However, this is fine, because the intended
  use of fxtr UI is that a coding agent will write type-id-specific
  rendering code, and register it with the dispatcher.
- **Strict immutability guarantees at the type level:** TS readonly
  hints help, but runtime is bring-your-own-discipline.

## What we'd add later if needed

If a future need pushes us toward more, options remain:

- Possibly, a wrapper around EntityRefStub which attaches a
  _runtime conversion/validation codec_, so that when you load its
  data, that data is actually immediately validated and possibly
  converted (with its conversion logic pushing down sub-codecs to
  the children)
- A stripped-down `bicodec`-cousin field-codec system (decode-only),
  or zod (or similar) integration, which would serve as the runtime
  codec representation.
- A type-id ↔ schema registry that does runtime validation at
  decode boundaries.

None of these are blockers for getting a working frontend up.

## Open question

### OQ-a — Should there be a structural-walk visitor for the future?

Even without schemas, we'll want a generic "walk a state value,
visit every stub" helper for the reducer. We need it anyway for
the pin / preserve diff. Just need to make sure it's exposed in a
form that's general enough for renderers to use too (e.g.
fallback dispatch's JsonTree-style walker).
