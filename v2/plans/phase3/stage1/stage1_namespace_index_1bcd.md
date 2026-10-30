# Stage 1, Part 1bcd: Indexer Types, Serialization, Parsing, and Unpack Logic

Implements the concrete indexer types end-to-end: serialization to/from fxtr entities,
expression parsing, and the `unpack_entity` logic that extracts (key_tuple, value) pairs from
raw CBOR entities.

Depends on: 1a (sql_expr.py) — already implemented.

## Overview

Three interleaved concerns, all in `store_indexing/indexers.py` (with a new
`store_indexing/path_parse.py` for the parser):

- **1c — Serialization**: Make indexer dataclasses storable/loadable as fxtr entities via
  mashumaro, using a discriminated union for path elements.
- **1b — Parsing**: Implement `parse()` and `query()` class methods that turn string
  expressions into indexer instances and constraints.
- **1d — Unpack logic**: Implement `unpack_entity()` which walks raw CBOR data following the
  path to extract index rows.

## 1c: Serialization

### Path element discriminated union

Replace the current raw `tuple[str | int | VarMarker | DerefMarker | TypedDerefMarker, ...]`
with a mashumaro discriminated union hierarchy.

**Base class** (`PathElement`):

```python
@dataclass(frozen=True)
class PathElement(DataClassDictMixin):
    class Config(BaseConfig):
        discriminator = Discriminator(
            field="_k",
            include_subtypes=True,
            variant_tagger_fn=lambda cls: cls.__dataclass_fields__["_k"].default,
        )
        serialization_strategy = {
            FxtrID: FxtrIDSerializationStrategy(strong=False),
            frozenset: FrozensetSerializationStrategy(),
        }
```

The `variant_tagger_fn` pulls the tag directly from the field's default value — single source
of truth, no redundant class attributes.

The `serialization_strategy` entries are needed so that `TypedDerefMarker.supported_types`
(which is `frozenset[FxtrID]`) serializes correctly with CBOR tags and canonical ordering.
Import the strategy classes from `entity_serdes`.

**Variant classes** — each has a `_k: Literal[TAG]` field with `init=False` whose default
value doubles as the discriminator tag:

| Class | Tag | Fields | CBOR example |
|-------|-----|--------|--------------|
| `PathKey` | `"k"` | `key: str` | `{"_k": "k", "key": "turns"}` |
| `PathIndex` | `"i"` | `index: int` | `{"_k": "i", "index": 3}` |
| `VarMarker` | `"v"` | *(none)* | `{"_k": "v"}` |
| `DerefMarker` | `"d"` | *(none)* | `{"_k": "d"}` |
| `TypedDerefMarker` | `"td"` | `supported_types: frozenset[FxtrID]`, `strict: bool` | `{"_k": "td", "supported_types": [...], "strict": true}` |

Each variant's `_k` field uses `Literal` for the tag type and derives the default from it:
```python
_k: Literal["k"] = dataclasses.field(default="k", init=False, repr=False, compare=False)
```

**Path field type change**: Both `TypedPathValueIndexer` and `TypedPathExistsIndexer` change
from `path: tuple[str | int | VarMarker | ...]` to `path: tuple[PathElement, ...]`.

### Indexer field changes: `initial_lookup` replaces `supported_types`

Both indexers lose their `supported_types` field. Instead, they gain:

```python
initial_lookup: DerefMarker | TypedDerefMarker
```

This field controls the initial entity type check before path walking:

- `DerefMarker()` → index all entities (no type filtering)
- `TypedDerefMarker(supported_types={...}, strict=True)` → require entity type matches,
  raise `ValueError` if not
- `TypedDerefMarker(supported_types={...}, strict=False)` → filter to matching types,
  silently skip non-matching entities (return `[]`)

### Two-axis strictness model

There are two independent strictness axes:

1. **Indexer-level `strict: bool`** — controls schema strictness during path walking:
   - Missing dict keys → `[]` (lax) or `ValueError` (strict)
   - Out-of-bounds list indices → `[]` (lax) or `ValueError` (strict)
   - VarMarker on non-container → `[]` (lax) or `ValueError` (strict)
   - Value type mismatch at end of path → skip (lax) or `ValueError` (strict)

2. **TypedDerefMarker `strict: bool`** — controls type filtering strictness (both for
   `initial_lookup` and mid-path derefs):
   - Entity type not in `supported_types` → `[]` (lax) or `ValueError` (strict)

These are independent. An indexer can be strict about schema but lax about type filtering
(e.g., "index everything with these fields, ignoring entities of other types"), or lax about
schema but strict about type filtering (e.g., "require this type but tolerate missing fields").

### Indexer type IDs

Add a hardcoded namespace UUID to `indexers.py`:

```python
_INDEXER_NAMESPACE_UUID = uuid.UUID("cef8b5a0-7c0d-4378-93aa-971c634d3853")
```

Then define type IDs:

```python
class TypedPathValueIndexer(DataclassEntity, Indexer,
    fxtr_type=define_type(_INDEXER_NAMESPACE_UUID, "typed_path_value_indexer")):
    ...

class TypedPathExistsIndexer(DataclassEntity, Indexer,
    fxtr_type=define_type(_INDEXER_NAMESPACE_UUID, "typed_path_exists_indexer")):
    ...
```

### WellKnownBuiltinIndexer

Already defined as `TypedPathValueIndexer | TypedPathExistsIndexer`. This union works as an
`EntityRef` target because mashumaro's discriminator on `DataclassEntity` routes to the correct
variant via `$fxtr.type`.

### Implementation steps

- [x] Add `PathElement` base class with Config (discriminator + serialization strategies)
- [x] Convert `VarMarker`, `DerefMarker`, `TypedDerefMarker` to `PathElement` subclasses with
  `_k` tag fields
- [x] Add `PathKey` and `PathIndex` wrapper classes
- [x] Add `_INDEXER_NAMESPACE_UUID` and `fxtr_type=` to both indexer classes
- [x] Replace `supported_types` with `initial_lookup` on both indexers
- [x] Update `path` field type on both indexers to `tuple[PathElement, ...]`
- [x] Write roundtrip serialization tests (see Tests section)

## 1b: Path expression parsing

### New file: `store_indexing/path_parse.py`

A hand-written character-by-character parser for path expressions.

### Grammar

Expressions always start with an initial deref (`!`), optionally with a type filter:

```
expression := initial_deref "." body
           |  initial_deref             -- exists-only, no path after deref

initial_deref := "!" typed_deref_suffix?
typed_deref_suffix := "(as " id_list ")"    -- strict typed deref
                   |  "(as? " id_list ")"   -- lax typed deref

body       := segment ("." segment)* (":" col_type)?

segment    := name bracket? deref?
name       := [a-zA-Z_][a-zA-Z0-9_]*
bracket    := "[" bracket_inner "]"
deref      := "!" typed_deref_suffix?

-- For parse() (unnamed vars):
bracket_inner := "*:" ws col_type
              |  integer

-- For query() (named vars):
bracket_inner := "$" varname ":" ws col_type
              |  integer

id_list    := py_string (ws "|" ws py_string)*
col_type   := "int" | "str" | "bool" | "bytes" | "float" | "FxtrID"
```

`ws` = optional whitespace. `py_string` = a Python string literal (parsed via `tokenize`).

### Parsing approach

Single function:

```python
def parse_path_expression(
    expr: str,
    *,
    named_vars: bool,
    require_value_type: bool,
) -> ParsedPathExpression
```

Where `ParsedPathExpression` is:

```python
@dataclass(frozen=True)
class ParsedPathExpression:
    initial_lookup: DerefMarker | TypedDerefMarker
    path: tuple[PathElement, ...]
    var_types: tuple[IndexerColumnType, ...]
    var_names: tuple[str, ...] | None  # None when named_vars=False
    value_type: IndexerColumnType | None  # None when require_value_type=False
```

**Character-by-character parser** using an index cursor (`pos: int`):

0. **Parse initial deref**: expect `!`, then:
   - If `(`: parse typed deref suffix:
     - If next is `as?`: lax mode (`strict=False`), parse id_list, expect `)`
     - If next is `as`: strict mode (`strict=True`), parse id_list, expect `)`
     - → `TypedDerefMarker(supported_types=frozenset(ids), strict=...)`
   - Otherwise: → `DerefMarker()`
   - If next is `.`: advance past `.`, continue to parse body
   - If end of string: done (empty path, for exists-only indexers)
1. **Parse segment name**: consume `[a-zA-Z_][a-zA-Z0-9_]*` → `PathKey(key=name)`
2. **If `[`**: consume bracket contents:
   - If `*` (unnamed): expect `*:`, parse col_type → append `VarMarker()`, record var_type
   - If `$` (named): consume `$varname:`, parse col_type → append `VarMarker()`, record
     var_name and var_type
   - If digit: consume integer → `PathIndex(index=n)`
   - Expect `]`
3. **If `!`**: consume deref (same typed_deref_suffix grammar as initial):
   - `!(as ...)` → `TypedDerefMarker(strict=True)`
   - `!(as? ...)` → `TypedDerefMarker(strict=False)`
   - `!` alone → `DerefMarker()`
4. **If `.`**: advance, go to step 1
5. **If `:`**: parse final col_type → value_type
6. **If end of string**: done

**Tokenize trick for quoted strings**: when the parser cursor reaches a quote character inside
`!(as ...)`, wrap the remaining string in a `StringIO`, call `tokenize.generate_tokens()`,
pull the first token (assert it's a STRING), use `ast.literal_eval()` on the token string to
get the Python string value. Advance `pos` by the length of the token (accounting for any
surrounding whitespace the tokenizer consumed). This handles escaping, triple-quotes, etc.
automatically.

**Error handling**: raise `ValueError` with a message indicating the position and what was
expected. Include the original expression and a caret pointing to the error position.

### Integration with indexer classes

**`TypedPathValueIndexer.parse(expression, *, strict=False)`**:

```python
parsed = parse_path_expression(expression, named_vars=False, require_value_type=True)
return TypedPathValueIndexer(
    initial_lookup=parsed.initial_lookup,
    strict=strict,
    path=parsed.path,
    var_types=parsed.var_types,
    value_type=parsed.value_type,
)
```

**`TypedPathValueIndexer.query(expression, where, *, strict=False)`**:

```python
parsed = parse_path_expression(expression, named_vars=True, require_value_type=True)
indexer = TypedPathValueIndexer(
    initial_lookup=parsed.initial_lookup,
    strict=strict,
    path=parsed.path,
    var_types=parsed.var_types,
    value_type=parsed.value_type,
)
return indexer.bind(parsed.var_names, where)
```

Same pattern for `TypedPathExistsIndexer` but with `require_value_type=False`.

### Implementation steps

- [x] Create `store_indexing/path_parse.py` with `parse_path_expression()`
- [x] Implement `TypedPathValueIndexer.parse()` and `.query()`
- [x] Implement `TypedPathExistsIndexer.parse()` and `.query()`
- [x] Write parser tests (see Tests section)

## 1d: Unpack logic

### Core algorithm

`unpack_entity` walks the raw CBOR data following the path elements, fanning out at
`VarMarker`s (list-monad style), and loading child entities at deref markers.

**Shared async helper function** (in `indexers.py` or a new `store_indexing/unpack.py`):

```python
async def _walk_path(
    data: Any,  # current CBOR value (dict, list, or primitive)
    path: tuple[PathElement, ...],
    path_idx: int,
    scope: FxtrScope,
    var_bindings: list[IndexerColumnValue],
    strict: bool,  # indexer-level schema strictness
) -> list[tuple[tuple[IndexerColumnValue, ...], Any]]:
    """Walk the path from path[path_idx] onward, returning (bindings, final_value) pairs."""
```

At each step:

- **`PathKey(key)`**: dict lookup. `data = data.get(key, MISSING)`. If missing → `[]` (lax) or
  `ValueError` (strict).
- **`PathIndex(index)`**: list index. `data = data[index]` with bounds check. If out of bounds
  → `[]` (lax) or `ValueError` (strict).
- **`VarMarker()`**: fan out over the current container:
  - If `data` is a `dict`: iterate over `data.keys()` (excluding `$fxtr.ctx` and `$fxtr.type`),
    bind each key, recurse with `data[key]`.
  - If `data` is a `list`: iterate over indices, bind each index, recurse with `data[index]`.
  - Otherwise: `[]` (lax) or `ValueError` (strict).
- **`DerefMarker()`**: `data` should be a CBOR FxtrID reference (`RawWeakFxtrID` or
  `RawStrongFxtrID`). Extract the `FxtrID`, load the entity via `await scope.load_cbor(fxtr_id)`,
  continue with the loaded CBOR data. If data is not a FxtrID reference → `[]` (lax) or
  `ValueError` (strict).
- **`TypedDerefMarker(supported_types, strict_deref)`**: like `DerefMarker` but after loading,
  check the entity's `$fxtr.type` against `supported_types`. If not matched → `[]` (uses
  **deref's own `strict`**, not indexer-level strict) or `ValueError` (if deref is strict).

When `path_idx == len(path)` (end of path):
- Return `[(tuple(var_bindings), data)]`

### Initial lookup check

Before walking the path, `unpack_entity` applies `self.initial_lookup`:

- **`DerefMarker()`**: no type check, proceed directly to path walking.
- **`TypedDerefMarker(supported_types, strict)`**: check the entity's `$fxtr.type`:

```python
entity_type_raw = entity_data.get("$fxtr.type")
if entity_type_raw is not None:
    entity_type_id = entity_type_raw.inner_id
    if entity_type_id not in initial_lookup.supported_types:
        if initial_lookup.strict:
            raise ValueError(f"Entity type {entity_type_id} not in supported types")
        return []
elif initial_lookup.strict:
    # No type on entity but strict typed deref requires one
    raise ValueError("Entity has no type but strict typed deref requires a match")
else:
    return []  # No type → can't match typed deref, skip
```

### Extracting FxtrID from CBOR references

In raw CBOR data, FxtrID references appear as `RawWeakFxtrID` or `RawStrongFxtrID` wrapper
objects. The `inner_id` field is already a `FxtrID`:

```python
if isinstance(value, (RawWeakFxtrID, RawStrongFxtrID)):
    fxtr_id = value.inner_id  # already a FxtrID
```

### Value type checking

At the end of the path (for `TypedPathValueIndexer`), check that the final value matches
`self.value_type`:

| IndexerColumnType | Expected Python types |
|-------------------|-----------------------|
| INT | `int` (not bool) |
| STR | `str` |
| BOOL | `bool` |
| BYTES | `bytes` |
| FLOAT | `float` or `int` (coerce int→float) |
| FXTR_ID | `RawWeakFxtrID` or `RawStrongFxtrID` (extract `.inner_id`) |

Strict indexers raise `ValueError` on type mismatch; lax indexers skip.

### TypedPathExistsIndexer.unpack_entity

Same path walking but when we reach the end of the path, return `True` as the value instead
of extracting a primitive. No value type check needed.

### Implementation steps

- [x] Implement `_walk_path()` async helper
- [x] Implement value type checking/coercion helper
- [x] Implement `TypedPathValueIndexer.unpack_entity()`
- [x] Implement `TypedPathExistsIndexer.unpack_entity()`
- [x] Write unpack tests (see Tests section)

## Tests

### Serialization tests (1c)

In `tests/test_store_indexing/test_indexer_serdes.py`:

- **PathElement roundtrip**: For each variant (PathKey, PathIndex, VarMarker, DerefMarker,
  TypedDerefMarker), serialize via `to_dict()` and deserialize via
  `PathElement.from_dict()`. Verify roundtrip equality.
- **TypedDerefMarker with FxtrIDs**: Verify that `supported_types` frozenset serializes to a
  sorted list of CBOR-tagged FxtrIDs and roundtrips correctly.
- **TypedPathValueIndexer store/load**: Create an indexer with initial_lookup, store it via
  `store_entity()` in a scope, load it back via `EntityRef.load()`, verify equality.
- **TypedPathExistsIndexer store/load**: Same as above.
- **WellKnownBuiltinIndexer union load**: Store a TypedPathValueIndexer and a
  TypedPathExistsIndexer, load both via `EntityRef[WellKnownBuiltinIndexer]`, verify correct
  types are returned.
- **Canonical determinism**: Create two identical indexers with different field construction
  order (e.g., different frozenset insertion order), verify they produce the same FxtrID.

### Parser tests (1b)

In `tests/test_store_indexing/test_path_parse.py`:

- **Simple path**: `"!.name: str"` → initial_lookup=DerefMarker(), path=(PathKey("name"),),
  value_type=STR
- **Dotted path**: `"!.foo.bar.baz: int"` → DerefMarker(), three PathKeys, value_type=INT
- **Unnamed variable**: `"!.items[*: int].name: str"` → DerefMarker(), PathKey, VarMarker,
  PathKey, var_types=(INT,), value_type=STR
- **Multiple unnamed vars**: `"!.turns[*: int].metrics[*: str].score: float"`
- **Named variables**: `"!.turns[$t: int].metrics[$m: str].score: float"` with named_vars=True
  → var_names=("t", "m")
- **Fixed index**: `"!.items[3].name: str"` → DerefMarker(), PathKey, PathIndex(3), PathKey
- **Mid-path deref marker**: `"!.ref!.name: str"` → DerefMarker() initial, PathKey, DerefMarker,
  PathKey
- **Mid-path typed deref**: `"!.ref!(as '!some.type' | '!other.type').name: str"` → PathKey,
  TypedDerefMarker(strict=True) with two FxtrIDs, PathKey
- **Mid-path lax typed deref**: `"!.ref!(as? '!some.type').name: str"` → PathKey,
  TypedDerefMarker(strict=False), PathKey
- **Strict initial typed deref**: `"!(as '!some.type').turns[*: int].name: str"` →
  initial_lookup=TypedDerefMarker(strict=True)
- **Lax initial typed deref**: `"!(as? '!some.type').turns[*: int].name: str"` →
  initial_lookup=TypedDerefMarker(strict=False)
- **Exists indexer (no value type)**: `"!.items[*: int]"` with require_value_type=False
- **Exists with typed deref only**: `"!(as '!some.type')"` → typed initial, empty path
- **parse() integration**: `TypedPathValueIndexer.parse("!.items[*: int].score: float")`
- **query() integration**: `TypedPathValueIndexer.query("!.items[$i: int].score: float", "value > 3")`
  returns an IndexerConstraint
- **Error cases**: missing initial `!`, missing type annotation, unclosed bracket, invalid
  col_type, duplicate var names, etc.

### Unpack tests (1d)

In `tests/test_store_indexing/test_unpack.py`:

These tests need a `FxtrScope` (or mock) for entity loading. Use the existing
`memory_store`/`active_arena` fixtures to create scopes with test entities.

- **Simple field extraction**: entity `{"$fxtr.ctx": ..., "$fxtr.type": ..., "name": "foo"}`,
  indexer with `initial_lookup=DerefMarker()`, path `"name: str"` → `[((), "foo")]`
- **Nested field**: path `"outer.inner: int"` → extracts nested value
- **Variable iteration (dict)**: path `"scores[*: str]: float"` on
  `{"scores": {"a": 1.0, "b": 2.0}}` → `[(("a",), 1.0), (("b",), 2.0)]`
- **Variable iteration (list)**: path `"items[*: int]: str"` on `{"items": ["x", "y"]}` →
  `[((0,), "x"), ((1,), "y")]`
- **Multiple variables**: path `"turns[*: int].metrics[*: str].score: float"` on nested
  structure
- **Mid-path deref marker**: entity has a FxtrID field, deref loads the child, continues into
  child's fields
- **Mid-path typed deref marker**: like deref but filters on `$fxtr.type` of the loaded entity
- **Initial typed deref (lax)**: `initial_lookup=TypedDerefMarker(types, strict=False)`, entity
  type not in `supported_types` → returns `[]`
- **Initial typed deref (strict)**: `initial_lookup=TypedDerefMarker(types, strict=True)`,
  entity type not in `supported_types` → raises `ValueError`
- **Initial DerefMarker**: `initial_lookup=DerefMarker()`, indexes entity regardless of type
- **Missing key (lax indexer)**: path refers to non-existent key → returns `[]`
- **Missing key (strict indexer)**: → raises `ValueError`
- **Lax indexer + strict typed deref**: schema miss returns `[]`, but wrong type raises
  `ValueError` (axes are independent)
- **Exists indexer**: same path walking but always returns `True` at end
- **Value type coercion**: int value with FLOAT value_type → coerced to float
- **FxtrID value extraction**: value at end of path is a CBOR FxtrID ref → extracted as FxtrID

## Implementation order

1. Serialization (1c) — path element types, fxtr_type IDs, field type changes
2. Serialization tests — verify roundtrip before building on top
3. Parser (1b) — `path_parse.py`
4. Parser integration — `parse()` and `query()` methods
5. Parser tests
6. Unpack logic (1d) — `_walk_path()`, `unpack_entity()` implementations
7. Unpack tests
8. Update `store_indexing/LOCAL_CODE_OVERVIEW.md`
9. Update `CODE_OVERVIEW.md`
