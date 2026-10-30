# Stage 5: Bicodec type inference improvements

## Problem

When using `bicodec.entity()`, TypeScript expands all structural types in tooltips/IDE
hover. For example, `FxtrBasicType.create(...)` shows:

```
(method) EntityDefinition<...>.create(fields: {...}): EntityInstance<{
    namespace_uuid: string;
    type_name: string;
}>
```

instead of something like `EntityInstance<FxtrBasicType>`. Deeply nested entity references
(e.g. `Article` with `Author` and `Tag` refs) produce enormous, unreadable tooltip expansions.

## Solution

Three complementary changes:

### 1. Rename `Infer` → `InferEntityFields` and return fields instead of instance

Currently: `Infer<D>` extracts `EntityInstance<T>` from `EntityDefinition<T>`.

Change: `InferEntityFields<D>` extracts just `T` (the fields type).

This enables the pattern where a type alias like `type FxtrBasicType = InferEntityFields<typeof _FxtrBasicType>`
represents the *fields* shape, and `EntityDefinition<FxtrBasicType>` naturally accepts it
as its type parameter (since `EntityDefinition<T>` is parameterized on fields).

### 2. Add `Expand<T>` to `EntityDefinition.create`'s parameter

```typescript
type Expand<T> = T extends infer U ? { [K in keyof U]: U[K] } : never;

export interface EntityDefinition<T> extends EntityLoadCodec<EntityInstance<T>> {
    create(fields: Expand<T>): EntityInstance<T>;
    ...
}
```

This forces TypeScript to expand the fields in `create()`'s parameter (so you can see what
to pass), while preserving the named alias in return types and `EntityRef` wrappers.

Result: `create(fields: { namespace_uuid: string; type_name: string }): EntityInstance<FxtrBasicType>`

### 3. Recommended definition pattern

```typescript
const _FxtrBasicType = bicodec.entity(trackBuiltinTypeId("basic_type"), {
    namespace_uuid: bicodec.string(),
    type_name: bicodec.string(),
});
export interface FxtrBasicType extends bicodec.InferEntityFields<typeof _FxtrBasicType> {}
export const FxtrBasicType: bicodec.EntityDefinition<FxtrBasicType> = _FxtrBasicType;
```

The `_`-prefixed const holds the inferred definition. The `interface extends` extracts
fields and gives them a name that TypeScript always preserves in tooltips (unlike `type =`
which gets expanded for conditional types). The exported const is annotated with the named
type, so TypeScript uses it in all downstream generics (return types,
`EntityRef<EntityInstance<FxtrBasicType>>`, etc.).

Important: we must use `interface extends` rather than `type =` for the fields type.
A `type` alias defined via a conditional type (which `InferEntityFields` is) gets expanded
by TypeScript in tooltips, defeating the purpose. An `interface extends` is always shown
by name. So the pattern uses `interface` everywhere.

## Files to change

- [x] `ts/packages/fxtr/src/common/bicodec.ts` — rename `Infer` → `InferEntityFields`, change it to return `T` not `EntityInstance<T>`, add `Expand<T>` helper, use `Expand<T>` in `EntityDefinition.create`, update `entity()` docstring with recommended pattern
- [x] `ts/packages/fxtr/src/index.ts` — update re-export from `Infer` → `InferEntityFields`
- [x] `ts/packages/fxtr/src/common/systemEntities.ts` — update import, keep `interface extends` pattern with new name
- [x] `ts/packages/examples/src/entities.ts` — adopt the `_`-prefixed + `interface extends` + re-export pattern for Author, Tag, Article
- [x] `ts/packages/fxtr/tests/common/bicodec.test.ts` — update `Score` type inference test: rename, fix compile-time checks (Score is now fields type, `EntityInstance<Score>` is the instance type)
- [x] `ts/packages/fxtr-http-client/tests/http/client.test.ts` — update `SimpleEntityT`/`EntityWithRefT` to use new name, adopt full pattern, fix `EntityRef` usages
- [x] `ts/packages/fxtr-http-server/tests/http/server.test.ts` — same as above
- [x] `ts/CODE_OVERVIEW.md` — update bicodec type inference docs

---

## Follow-up: Named type = instance type (not fields)

### Problem with original design

The original `InferEntityFields` pattern made the named type represent the *fields* type. This
meant you had to write `EntityRef<EntityInstance<Foo>>` everywhere instead of `EntityRef<Foo>`,
which was verbose and leaked the internal `EntityInstance` wrapper into user-facing code.

### Solution: `InferEntity` + phantom fields marker

Reparameterize so the named type IS the full entity instance. Three key changes:

**1. Phantom field on `EntityInstance<T>`** for type recovery, plus polymorphic `store()`:

```typescript
declare const __PHANTOM_ENTITY_FIELDS: unique symbol;

interface _EntityInstance<T> extends Entity {
    readonly [FXTR_CTX]?: FxtrID;
    store(scope: FxtrScope): Promise<EntityRef<this>>;
    readonly [__PHANTOM_ENTITY_FIELDS]?: T;  // phantom — never set at runtime
}

export type EntityInstance<T> = T & _EntityInstance<T>;

export type FieldsOf<I> = I extends { readonly [__PHANTOM_ENTITY_FIELDS]?: infer T }
    ? T : never;
```

The `declare const` symbol exists only at the type level. The `?` optional marker means the
property is never actually present at runtime. `FieldsOf<I>` recovers `T` from the phantom.

The `_EntityInstance` interface uses `store(): Promise<EntityRef<this>>` — the polymorphic `this`
means that when a named type like `MyEntity` extends `EntityInstance<...>`, hovering over
`.store()` shows `Promise<EntityRef<MyEntity>>` rather than `Promise<EntityRef<EntityInstance<...>>>`.

**2. `EntityDefinition<I>` parameterized on instance type:**

```typescript
export interface EntityDefinition<I extends EntityInstance<unknown>>
    extends EntityLoadCodec<I> {
    create(fields: Expand<FieldsOf<I>>): I;
    readonly typeId: FxtrID | Promise<FxtrID>;
}
```

`create()` uses `FieldsOf<I>` to extract just the fields for the parameter, while the return
type is the full named instance type `I`.

**3. `InferEntity<D>` replaces `InferEntityFields<D>`:**

```typescript
export type InferEntity<D> = D extends EntityDefinition<infer I> ? I : never;
```

Returns the instance type, not the fields type.

### Updated definition pattern

```typescript
const _MyEntity = bicodec.entity(defineType("ns", "MyEntity"), {
    name: bicodec.string(),
    value: bicodec.number(),
});
export interface MyEntity extends bicodec.InferEntity<typeof _MyEntity> {}
export const MyEntity: bicodec.EntityDefinition<MyEntity> = _MyEntity;
```

Now `MyEntity` IS the instance type, so:
- `EntityRef<MyEntity>` instead of `EntityRef<EntityInstance<MyEntity>>`
- `bicodec.child(MyEntity)` gives `FieldCodec<MyEntity>`
- `MyEntity.create(...)` parameter shows expanded fields, return type shows `MyEntity`

### Implementation note

Inside `entity()`, TypeScript can't prove `FieldsOf<EntityInstance<Fields>> = Fields` at the
generic level (conditional types defer on generic inputs), so the return uses
`as EntityDefinition<Instance>`. This is safe — it's internal to the factory function.

### Files changed

- [x] `ts/packages/fxtr/src/common/bicodec.ts` — add phantom, reparameterize `EntityDefinition`, rename `InferEntityFields` → `InferEntity`, add `FieldsOf`
- [x] `ts/packages/fxtr/src/index.ts` — update exports
- [x] `ts/packages/fxtr/src/common/systemEntities.ts` — use `InferEntity`
- [x] `ts/packages/fxtr/tests/common/bicodec.test.ts` — update type inference tests
- [x] `ts/packages/examples/src/entities.ts` — use `InferEntity`
- [x] `ts/packages/examples/src/web/ArticleView.tsx` — remove `EntityInstance` import, simplify all type annotations
- [x] `ts/packages/fxtr-http-client/tests/http/client.test.ts` — use `InferEntity`, simplify `EntityRef` types
- [x] `ts/packages/fxtr-http-server/tests/http/server.test.ts` — same as above
