# Slice DSL sketch (SUPERSEDED — kept for reference)

**Superseded 2026-07-02** by the capability-view rewrite calculus
(`fxtr_experiment/views.py`): slices are built by chaining pure rewrites
(`rename_axes`, `at`) and opens on view objects, so a string DSL is just a
worse spelling of a Python method chain. The notation below may still be
useful as *display* syntax for rendering stored rules; revisit then.

A compact string syntax for writing draft slices, with a bidirectional
parse/stringify to/from the `draft.py` dataclasses (the dataclasses stay the
canonical parse target; the DSL is pure surface syntax). Motivation: building
nested `BoundAxis`/`frozendict` structures by hand is noisy, and we already
gravitated to notation like this in `example_mh_pilot.md`.

## Sketch

```
slice   := root step* target?
root    := "@" NAME            # InputRoot(NAME)
         | "@own"              # OwnInternalsRoot
step    := cell? "/" KEY       # DraftStep(cell, KEY)
cell    := "{" [bind ("," bind)*] "}"     # omissible when the grid is zero-axis
target  := "[" [bind ("," bind)*] "]"     # target_axes; omissible if empty
bind    := AXIS "=" expr
expr    := STRING | INT       # LiteralCoord
         | "$" PARAM          # BoundAxis(PARAM) — binder = the draft's axes
         | "*"                # KeepAxis (allowed in cell and target binds)
```

The slice *mode* is not part of the string — it stays a structured field on
`SliceBindingDraft` (it's an access declaration, not location syntax).

## Examples (from the MH-pilot example)

```
@fns[fn="rollout"]                                     # single handle cell
@subjects[subject=$subject]                            # vmap-bound
@workspace/grades[subject=$subject, usim=$usim, judge=*]   # kept axis
@own/judge_calls                                       # checkpoint grid (0-axis target)
@workspace/gen_usims[usim=*]                           # created-grid reference
@variants{variant=*}/agg                               # kept STEP axis: one child key
                                                       #   spanning all subtree cells
```

(`@workspace/...` omits `{}` because the pre-sliced workspace capability is
zero-axis from the task's point of view.)

## Notes / open points

- Axis names in `cell`/`target` binds resolve against the addressed
  capability's local axis names; `$param` resolves against the draft's own
  `axes` binder. Kinds are never declared in the DSL — they come from the
  binder / target configs at canonicalization.
- Stringify should round-trip exactly (canonical field order: cell binds and
  target binds in the addressed grid's axis order).
- Possible later extension: a rule-level form (`name: mode slice` lines) so a
  whole `TaskRuleDraft` can be written as a small block.
- Deferred until the canonicalizer exists; revisit ergonomics then.
