# Stage 13post1 — followups

Things we might want to do later; deliberately out of scope for the chunk that
surfaced them.

## Entity-wrap channel payloads instead of manual closure-walking (from 1b)

Today the engine manages message/status/final **residency** by walking the encoded
`FxtrServiceData` for `FxtrID` / `EntityRef` leaves (`_iter_data_leaves` +
`_copy_closure` + `_has_entity_leaves`) and copying each leaf into the appropriate
per-node scope.

**Idea (Daniel):** define a lightweight entity, e.g.

```python
class _ChannelPayload(DataclassEntity, fxtr_type=...):
    value: FxtrChannelData
```

and let the entity/scope machinery manage the closure instead of walking it:

- In the **writer dispatch**, the value's leaves are already tracked in the dispatch
  input scope, so `_ChannelPayload(value=encoded).store(input_scope)` is cheap (deps
  present). Then copy that **one** payload ref into the node's residency scope
  (`copy_from_scope` walks the payload's closure internally) — no leaf-walk here.
- The **reader** side loads the payload entity and returns its inner `value` — so the
  values delivered/returned to consumers are still inline (observable wire shape
  unchanged for `iter_channel` / typed subscribers / the frontend).

**Why deferred (revisit):** it's a real alternative, but not a clear net
simplification:
- The node `log` becomes a list of payload refs, so **snapshots** (`peek_full`,
  `subscribe_*` seeds) must *load* the payloads to rebuild inline envelopes (async
  loads where today it's a dict copy), and the live `emit` publish still needs the
  inline envelope built in-hand.
- Adds a `store()` per emit/status/final on the hot path even for ref-free payloads
  (the common scheduler case), where the current leaf-walker does nothing.
- Trades a ~15-line walker (the same pattern the old `streams.py` used) for an entity
  type + store/load plumbing.

If we later find the closure-walking is a maintenance or correctness pain (e.g. when
adding new payload shapes), revisit — the writer-dispatch-store + single-copy insight
is the key enabler.

## Confusing `EntityRef[Entity]` decode error (from 1b)

While building the engine, annotating an impl arg as `EntityRef[Entity]` (intending a
type-erased "ref to some entity") failed to decode with:

> `Field "tag" of type EntityRef[Entity] in _WriterImpl__start__Args has invalid
> value <FxtrID …>`

That message points at the *value* (a `FxtrID`), which reads as "the wire produced
the wrong value type," and sent the investigation toward the encoding — when the
actual design is: at the **service-dialect** layer the bare `FxtrID` *is* the
type-erased carrier, and `EntityRef[T]` is the typed view requiring a **concrete**
`T` (the dialect deliberately keeps typed/untyped translation in one place). The fix
in the channel engine was to type the impl's tag arg as `FxtrID` (the typed reader
proxy reattaches `EntityRef[T]`), which is correct — but the error did not guide us
there.

**Why it's principled (the model I initially got wrong):** `load` dispatches on the
*annotation object* (`T` from `EntityRef[T]`), not on the stored `$fxtr.type` —
necessarily, since there's no global `$fxtr.type` → Python-type map and the Python
type can carry type variables the `$fxtr.type` doesn't encode. So `FxtrID` is the
type-erased carrier (id, no type to load as), `EntityRef[T]` is the typed view
(`T` must have a load handler), and `EntityRef[Entity]` is a contradiction: it claims
loadability but the abstract `Entity` has no handler. The loadable *wildcard* is
`UnknownEntity` / `LoadAsUnknownByDefault` (a concrete fallback handler), **not**
`Entity`. Easy to conflate the two.

Worth investigating (low priority):
- Why the message surfaces as "invalid value `<FxtrID>`" (pointing at the value)
  rather than something annotation-directed. Likely mashumaro wrapping the strategy's
  own `TypeError` with its generic field-level message — may be awkward to improve.
- **Possible nicer design (Daniel):** make `EntityRef[Entity]` a *valid* value that
  simply can't be `load`ed, raising a specific error *at load time* ("can't pick a
  load handler without a more concrete type annotation"). That matches the intuition
  of a deferred-type ref and surfaces the real rule (load dispatches on the
  annotation) at the actionable spot, instead of a dialect-boundary decode rejection.
