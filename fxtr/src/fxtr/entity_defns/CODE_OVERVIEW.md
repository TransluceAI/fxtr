# Entity definitions, arrays, and queries — code overview

The value types fxtr programs are written in, built on the entity core
(`../core/CODE_OVERVIEW.md`). This covers `entity_defns/` (the dataclass layer,
`UnknownEntity`, `DynamicData`, schemas, arrays and their Arrow form, query descriptions and SQL
checking), `../duckdb_engine/` (running queries in DuckDB), and `../array_sql.py` (`fxtr.sql`). A
workflow's query nodes, which run these queries on the host, are the runner's
(`../runner/CODE_OVERVIEW.md`). The query conventions users write against are in the
documentation's array query reference, `docs/fxtr/reference/array-queries.mdx`.

```
fxtr/
├── array_sql.py            fxtr.sql: one SELECT over named arrays, local or host-side
├── entity_defns/           concrete Entity types and the dataclass layer
│   ├── unknown_entity.py   UnknownEntity fallback (wildcard loader)
│   ├── dataclass_bases.py  the class-side surface: TaggedDataclass, the serialization-hook
│   │                       protocol and its default hook builders
│   ├── converter.py        the cattrs-backed FxtrConverter (strict hooks, DEFAULT_CONVERTER)
│   ├── dataclass_entity.py DataclassEntity + its loaders and registry factory
│   ├── dynamic_data.py     DynamicData: a tagged value held undecoded, read back as a
│   │                       requested type
│   ├── schema.py           Schema (the regular value type system), the constructors and the
│   │                       annotation grammar, presentation, coercion, inference, assignability
│   ├── array.py            Array (sparse named arrays: dims + one value schema), its _Backing
│   │                       (Python rows, Arrow table, or both), Dim, ArrayType, PartialArrayType,
│   │                       join_keys, and the array operations
│   ├── query.py            the query builder's expressions and operation type derivation
│   ├── query_plan.py       query plan nodes, and PlannedQuery: a plan with the arrays it reads
│   │                       and its checked result type
│   ├── sql.py              read-only SQL parsing, the check that SQL reads only its named tables,
│   │                       tokenizer-based parameter binding, and immutable canonical parameters
│   └── arrow.py            versioned ArrayType↔Arrow schemas, bounded conversion both ways,
│                           validation, the array operations over Arrow, canonical CBOR encoding
│                           and content IDs, and decoding canonical data straight into Arrow
└── duckdb_engine/          running query plans in DuckDB; uses the entity layer and nothing in
    │                       the runner (Array's query methods import it back)
    ├── compiler.py         typed-plan compilation to parameterized DuckDB SQL
    ├── executor.py         DuckDBArrowExecutor: interruptible DuckDB execution over Arrow tables,
    │                       with result validation and optional limits
    └── local_query.py      ArrayQuery, the lazy query Array.query() returns: it reads its source
                            arrays' Arrow tables and collects an array holding DuckDB's table;
                            and query_arrays, how a workflow's query node is computed
```

## The dataclass layer

**`UnknownEntity`** (`entity_defns/unknown_entity.py`) — a fallback `Entity`
wrapping raw frozen (tagged) data, loaded via the shared
`UNKNOWN_ENTITY_LOADER`, a *wildcard* loader whose support checks accept every
type tag. It round-trips arbitrary content faithfully. The module registers
the wildcard for `UnknownEntity` in the default registry.

**The dataclass layer** (`entity_defns/dataclass_bases.py`, `converter.py`,
`dataclass_entity.py`) — the convenient way to define entities.
**`TaggedDataclass`** (`dataclass_bases.py`, the class-side surface)
is a frozen dataclass carrying a `$type` tag declared via a class keyword,
`fxtr_type="<type id>"` (class-level metadata, not a field; spliced in on
serialize, validated and stripped on deserialize; not inherited by subclasses
that don't re-declare). Tagged
dataclasses are *not* storable: they serialize inline and act as members of
tagged unions at any depth of a value — tagging is deliberately independent
of storage. **`DataclassEntity`** (`dataclass_entity.py`) is a
`TaggedDataclass` that is also an `Entity`: storable with zero per-class
registration (one `DataclassEntityLoaderFactory` is registered for the base
class with `allow_implicit_overrides=True`, covering all subclasses via MRO
walking, and overridable per class or alias). Serialization runs through the
cattrs-backed **`FxtrConverter`** (`converter.py`, `DEFAULT_CONVERTER`):
maximally strict decoding (extra keys, primitive coercions, missing declared
fields, and wrong/missing `$type` all rejected), canonical sorted
`set`/`frozenset` serialization, `frozendict` fields, unions of primitives (`int | str`,
structured by exact runtime type), native recursive `JSONValue` / `JSONObject`
fields and their frozen `FrozenJSONValue` / `FrozenJSONObject` counterparts
(`core/json.py`, preserving JSON scalar types, rejecting bytes and references, and
requiring the alias's own container shapes on save), element hooks taken from the
declared element type inside `frozendict`/`set` fields as in `list`/`dict` ones,
generic dataclasses
with parameter substitution, and `BoundID[...]` fields whose loaders are
resolved from the field annotation through the registry. Serialization is a
pure function of the runtime value: unstructuring re-dispatches on each
value's runtime type rather than the declared slot type, so a subclass or a
value in a base-typed or defaulted-type-parameter slot keeps its own `$type`
and fields instead of being coerced to the slot's declared type. Since the
encoder accepts what the decoder may reject, `DataclassEntity` serializes
with a **load-back check**: it decodes its data back as its class and
requires re-encoding to reproduce it, so an undecodable value fails when
stored (or when its `content_id` is computed) rather than when next read.
Generic classes are not checked (loading code chooses their type arguments),
and a class opts out with `_fxtr_check_decodes_on_store = False`, as `Array`
does; the `DataclassEntity` docstring has the rule.
Before building any dataclass's hooks, the converter resolves its field
annotations with `typing.get_type_hints` (written back into the dataclass
fields), so quoted names work anywhere in an annotation (`tuple["Group", ...]`),
with or without `from __future__ import annotations`.
The converter module's docstring states the invariants governing
generic type parameters and serialization. Classes customize their own
serialization through the **serialization-hook protocol**
(`dataclass_bases.py`): a classmethod per direction —
`__fxtr_make_structure_hook__` / `__fxtr_make_unstructure_hook__` — each
receiving the raw `cattrs.Converter` plus the annotation and returning that
direction's content hook. cattrs' gen API is the customization vocabulary,
with `make_dataclass_structure_hook` / `make_dataclass_unstructure_hook` as
the default building blocks (generated dict hooks plus strict field
presence on decode). The two directions must be customized together —
a tagged class body overriding one without the other is rejected at class
definition, and an untagged class defining either must define both — so
serialize and deserialize cannot silently diverge. For tagged classes the
framework's `$type` layer wraps non-bypassably *around* the returned hooks
(a class controls its field format, never its tag mechanics); any other
class defining the pair — e.g. a custom leaf value type — is dispatched
through it directly, its hooks producing the whole wire form. The full
contract lives in the `dataclass_bases` module docstring. Error policy:
anything escaping a conversion entry point is a **`ConversionError`**
(`dataclass_bases.py`; its docstring is the source of truth) — fxtr's own
strictness errors (`StrictTypeError`) subclass it and propagate as
themselves, while foreign failures (cattrs' field-validation groups,
hook-build errors like an undeclared tag) are wrapped with the original as
`__cause__`, a validation group's message listing each failure with its path
in the value; class-definition-time errors stay plain `TypeError`.

**`DynamicData`** (`entity_defns/dynamic_data.py`) — the inline counterpart of
`UnknownEntity`: a field type for a slot whose value type is decided by the
producer rather than the schema (metadata, extension points). It holds a
tagged value's serialized form, `$type` tag included, undecoded (`type` reads
the tag); `from_tagged(value)` builds one from a `TaggedDataclass`, and `as_type(Cls)` /
`try_as_type(Cls)` decode it strictly through the converter once the consumer
knows what it expects (a tag mismatch raises, or yields `None` from the `try_`
variant; `is_type` is the tag check alone). Its wire form is *transparent*: a
`DynamicData` field serializes to exactly the tagged mapping the concrete class
would have produced, so annotating a slot `DynamicData`, `Cls`, or a tagged
union changes only the Python-side loading policy, never the content id. It is
an untagged class implementing the serialization-hook protocol (not a
`TaggedDataclass`), so it cannot be a member of a tagged union.

## Schemas

**Schemas** (`entity_defns/schema.py`) — the value type system shared by arrays,
steps, and workflows. A `Schema` is regular: leaves
`int`/`float`/`bool`/`str`/`bytes` (`LeafSchema`), entity ids (`IdSchema`: an
entity id stored as a bare reference, optionally annotated with the type id
the entity is expected to have), lists (`ListSchema`),
structs with fixed unordered fields (`StructSchema`), and a non-nestable
nullable wrapper (`NullableSchema`). Schemas are written in Python only: read
off a type annotation by `schema_from_annotation` (the primary way, through a
body's annotations or an `Array[X]` parameter) or built with the constructors
`id_of` / `list_of` / `struct_of` / `optional` for what no annotation
expresses. There is no text form; `str(schema)` is display only. Every
argument that takes a schema (`SchemaSpec`, normalized by `as_schema`) accepts
a `Schema` or an annotation, and rejects a string with a pointer to those. The
annotation grammar is a strict whitelist of the
forms whose *presented* value has the annotated type: the five leaf types,
`Sequence[X]` / `tuple[X, ...]` / `list[X]`, `X | None`, `EntityID`,
`BoundID[T]` for an entity class `T`, and total TypedDicts (from `typing` or
`typing_extensions`, every key required, never `closed=False` or
`extra_items`); everything else is a `SchemaError` naming what to write instead.
A PEP 695 ``type`` alias in any of these positions is expanded first
(`expand_type_alias`, substituting its parameters), so a user alias reads like
the type it names.
Schemas also carry **presentation**, off the wire and excluded from equality
and hashing (new fields are added last so positional `match` patterns keep
working): `IdSchema.loader` (the `EntityLoader` values are handed out bound
to; `id_of(T)` fills it from the registry, with the type id from
`T.fxtr_type_id()` or the loader's `static_type_id`, untyped but still loaded
for a class such as `UnknownEntity` that has no static type),
`StructSchema.python_type` (a TypedDict class, meaning values present as
dicts), and `ListSchema.present_as` (`tuple`, the default, or `list`).
`needs_presentation` says whether any of these are set, and `strip_presentation`
removes them all (leaving an equal, wire-identical schema) — what the runner
hands across the worker/host boundary so backends see presentation-free types.
An id's type is
an annotation only (checking a value never loads the referenced entity); a
typed id's wire form is `{"id": "<type id>"}`.
`coerce_value` checks a value strictly (no coercion, matching the converter's
policy) and freezes it (tuples/frozendicts with fields sorted by name); an id
accepts an `EntityID` or a `BoundID` (keeping only the id).
`infer_schema` infers a schema from a value (`None` among list
elements widens the element to nullable; all-null or empty positions are
errors, and so are bare entity ids, since an id does not determine its
entity's type; a `BoundID` whose loader has a `static_type_id` infers the
typed id with that loader); `is_assignable(provided, expected)` is exact match
except that a non-nullable schema fits its nullable counterpart and a typed id
fits the untyped `id`, recursively.

## Arrays

**Arrays** (`entity_defns/array.py`) — `Array` is a `DataclassEntity` that is a
*sparse named array*: ordered, named `Dim`s (each with `int` or `str` keys),
unique keys (one value per dim), and a single anonymous value per key described
by one `Schema`. Logically `{(k1, ..., kn): value}`; its static type is a
`ArrayType` (dims plus schema; an `ArrayTypeSpec` is an `ArrayType`, a
`(dims, schema)` pair such as `("[prompt: int, sample: int]", str)` with the
dims as a string and the schema a `SchemaSpec`, or a bare schema spec for a
scalar; dims strings, parsed by `parse_dims`, are the one remaining text form). A
scalar is an array with no dims holding exactly one value, a conventional array
is a struct-valued one, and ragged/nested data is an array with more dims. Dim
order is presentation only (it fixes key-tuple and canonical row order, and
arrays differing only in dim order are unequal; `transpose` reorders dims) —
every operation matches dims *by name*. `join_keys` lines arrays up along shared dims, broadcasting arrays
that lack a dim, with `join="exact"` (key sets must agree), `"inner"`
(intersect), or, for `combine` only, `"outer"` (union, missing values `None`
and every field nullable); the operations built on the model are `combine`
(zip arrays into a struct-valued one over the union of dims) and `stack_over`
(add leading dims over same-typed arrays keyed by tuples) and its one-dim case
`stack`, while `sel` (slice at dim values, dropping those dims), `rename`
(rename dims), and `field` (project a struct field) are query shortcuts (see
below). Each has a static counterpart
(`sel_type`, `rename_type`, `transpose_type`, `field_type`, `combine_type`,
`stack_over_type`, `stack_type`, plus `stack_dim` for the kind of a stacked
dim) that computes the result type without data. The canonical rows
(`raw_rows`) are validated, frozen, and sorted by key, so equal arrays have
equal content ids however built, and loading rejects non-canonical data.

An array holds its canonical rows, its canonical Arrow table, or both, in a
private `_Backing`. The public constructors build rows; a query result, an
array loaded from stored or received canonical data, and an operation on an array holding only Arrow build a table
(`Array._from_arrow`). The missing form is made when something first needs
it, once, under a lock, and kept: the first query converts rows to Arrow,
and `raw_rows` decodes a table into rows. Iterating (`items`, `values`,
`raw_items`, ...) decodes a table a batch at a time and keeps nothing,
`len` and key membership read only the key columns, and indexing decodes
one row. Entity IDs are held by content in either form. `transpose`,
`retype`, `stack_over` and `combine` have one implementation, on Arrow (in
`arrow.py`): an input holding rows converts to Arrow first (once, and keeps
the table), and the result holds only its table, decoded when read. The Arrow
layout of a type (`arrow_columns`, `array_arrow_schema`) is computed once per
type, which is most of what a small array's Arrow work would otherwise cost,
and `stack_arrow_tables` builds each column once for all its parts, a mapped
step's results being thousands of one-row parts. Retyping to an equal type and
`strip_presentation` share the backing. Equality is Python value equality
whichever form either side holds (rows compared a batch at a time when one
side has none), and the hash is built from fixed-size chunks of rows, so it
is the same for either form. `content_id()` is computed once and kept; for
an array holding only Arrow it hashes the canonical bytes written from the
table. Storing any array encodes its data value. A copy of an array is the array; pickling keeps the
form it was built in.
What an array *hands out* is the **presented** form of each value
(`present_value`, driven by the schema's presentation): ids whose schema has
a loader come back as `BoundID`s, structs declared by a TypedDict as fresh
dicts, lists declared `list` as fresh lists, everything else frozen as stored.
`Array[V]` is generic (covariantly) in the *presented* value type, so
`Array[BoundID[Bio]]` and `Array[SomeTypedDict]` are honest; `V` flows through
`rows`, `values`, `items`, `item`, indexing, and the constructors, while
`raw_rows`, `raw_values`, `raw_items`, `raw_records` give the frozen form
(`records` stays a frozendict, with presented field values, since dim keys
are mixed in). Presentation is off the wire, so it never affects equality or
content ids. Constructors: `scalar`, `from_items`, `from_records` (flat
records for a struct-valued array), and the schema-inferring `infer_scalar`,
`infer_from_items`, `infer_from_records`. `Array.retype(type)` is the one
operation behind **check, then adopt the declared type**: it requires the
array's type to be assignable to `type` and returns the same data under
`type`'s dims (transposing if the order differs), schema, and presentation.
Loading with an `Array[X]` annotation (`X` concrete: not a TypeVar, `Any`, or
the `FrozenValue` default) checks the stored schema against
`schema_from_annotation(X)` and adopts it, so `load(ref,
as_type=Array[BoundID[Bio]])` yields bound ids. `PartialArrayType` is an
array type whose value schema may be left out (a dims string alone,
`"[prompt: int, sample: int]"`, or a `(dims, None)` pair), for *asserting* a
type declared elsewhere: `matches`/`check` require
the same dims **in order** and an equal schema when one is given —
deliberately stricter than `is_assignable_array`, which compares dims as a
set. `Array.expect_type(spec)` (and `ArrayHandle.expect_type`) checks one and
returns the receiver, retyped to the spec's schema when it gives one (so an
`expect_type` naming `BoundID[Bio]` adds the loader), so the assertion can
be written inline on the call that produced it.

## Queries

`entity_defns/query.py` defines immutable query descriptions built with `dim`, `value`, and
`literal`; numeric arithmetic; strict primitive casts; null replacement; membership, range,
comparison, and null predicates; `when(...).then(...).otherwise(...)` conditionals; sort fields;
bare row `count()`; and expression aggregation methods for non-null/distinct counts, sums, means,
extrema, sample deviation/variance, and continuous quantiles. Predicates are Boolean value
expressions, so they can also be selected or aggregated. Runtime type derivation promotes mixed
numeric expressions to floats, tracks nullability, requires compatible conditional/coalesce
branches, and keeps entity IDs out of arithmetic and casts.
`dim("name")` always selects a dimension; `value()` selects the whole row value, and
`value().field("name")` selects a struct field. Repeated `field` calls traverse nested structs;
each name is literal, including dots, and `value()` takes no arguments. Dimension and
value-field names can overlap, and no expression guesses between them. Join equality keys
are `JoinKey` objects or explicit pairs of columns, not name mappings.
The type functions validate a query and derive its result `ArrayType` without reading rows.
Computed expressions are accepted by selection, filtering, aggregation inputs, integer/string
grouping keys, and limit ordering. Null predicates retain SQL three-valued behavior until the
outer filter converts null to a non-match.
`entity_defns/query_plan.py` holds the plan nodes and `PlannedQuery`, which keeps a plan
together with the arrays its `QuerySource` nodes index and its checked result type. Local
queries and a workflow's handles each wrap one, so combining two queries (a join, or SQL with named
tables) renumbers sources in one place. `QueryAggregate.group_by` is an ordered tuple,
because its names become the result's dimensions in order. `QuerySelect` constructs a struct value from a mapping of names to expressions;
`QuerySelectValue` replaces the complete value with one expression. Both retain the input
dimensions. `Array.filter`, `select`, `limit`, `aggregate`, `join`, `sel`, `rename`, and
`field` return an `ArrayQuery` from `duckdb_engine/local_query.py`, delegating to the same
methods on `Array.query()`; SQL has no `Array` method and enters through `fxtr.sql`. Unlike `Array`'s
other methods, which return an array at once, these only describe a query. `field(name)` on a local query is `select` of that field.
DuckDB matches names without regard to case: physical Arrow column names are distinct
ignoring case, and a type with two dims, or two fields of one struct, that differ only by
case is rejected when a query over it is built. Arithmetic and casts are the ones DuckDB runs for the
same SQL: `//` truncates toward zero, `%` takes the dividend's sign, `//` between floats
divides, `cast(int)` rounds, and an integer compared with a float is widened to a double. A
zero divisor gives infinity or null, which fails when the result is checked against its type. Strings order in comparisons as they do in `min`, `max`, and `order_by`.
`limit(ties="all")` requires an `order_by`, which accepts one key or several, each a bare
expression (ascending) or an `asc`/`desc` wrapper. A DuckDB failure reaches the caller as
`QueryError`; for a query-builder query the message omits the SQL fxtr generated.
`filter` is the row-filtering method on concrete arrays, local queries, and workflow handles.
False and null predicates remove rows.
Expressions address the current query's output value, and operations return independent queries.
The array query reference (`docs/fxtr/reference/array-queries.mdx`) describes the
Polars/Ibis-inspired methods and the differences
required by fxtr's keyed arrays: explicit dimension/value references, key-preserving struct
whole-value or struct projection, integer/string grouping dimensions, nested inner-join values,
and canonical key order.
These methods build a lazy plan; `Array.query(limits=..., query_limiter=...)` configures local
execution explicitly. Only `collect()`
or `item()` executes DuckDB locally, and each call executes the plan again: a
`ArrayQuery` holds a plan and no result, so keeping one alive keeps no rows alive, and a
query built from another always extends its plan. A query's sources are the arrays
themselves: the first query over a Python array converts it to Arrow and keeps the table on
it. The array `collect()` returns holds DuckDB's table and no Python rows, so the way to use
a result again is to keep that array and query it. A query that only drops rows (filters, limits, and
an identity `select(value())`; see `row_preserving_source`) asks DuckDB for the surviving
keys and takes the complete rows from the input's Arrow table. When that input holds Python
rows, the result keeps a weak reference to it: if the input is still alive when the result's
rows are first needed, its row objects are shared, about fifteen times faster than decoding
them for a large result, and the result never keeps the input alive. Every other result is
decoded from Arrow when read. Row reuse preserves entity-ID membership and the restoration of
each input's own reference; a bare ID requiring a richer storage reference uses the decoder. A workflow's query node runs `query_arrays` over its inputs' tables, and its value
holds only the result table.

`DuckDBArrowExecutor` retains one idle connection for the next query. Queries derived from one
`Array.query()` root share that root's executor, and every local query that sets no limits of its
own runs on the process's one `default_executor()`, so a program holding many query objects, or
many queried arrays, holds one idle connection (about 3 MB) and not one each; a root given
`limits` or a `query_limiter` has its own. Running queries own separate connections; input
registrations are removed before reuse, and native failures, timeouts, and cancellation discard
their connection. A run forgets its connection before the connection can serve another run, so
cancelling a finished run cannot interrupt someone else's query; `close_idle_connection()`
releases the idle one. A workflow's query nodes run on an executor of their own, on one DuckDB
thread.
Local queries require neither content-ID computation nor CBOR/disk
storage. A shared optional capacity limiter bounds several local query roots.

## Arrow

`entity_defns/arrow.py` defines the versioned Arrow schema for every `ArrayType`, and is where
code outside `entity_defns` asks an array for what its insides hold: `array_arrow_table`,
`array_holds_rows`, `array_holds_arrow_table`, and `array_from_validated_table`, so the engines
never unpack an `Array` themselves. Python arrays
convert in bounded record batches. Arrow builders consume frozen structs and lists directly;
entity IDs and empty structs are translated to their binary and false-boolean representations.
Typed builders check integer ranges, and vectorized float checks reject non-finite values.
Arrow batches validate schema, visible nested nullability, finite numbers, CID format,
empty-struct placeholders, key order/uniqueness, and scalar cardinality. Nullable parents hide
their child slots. Valid batches can be hashed exactly as the canonical DAG-CBOR bytes of
ordinary `Array` serialization (`arrow_table_content_id`). Empty structs use false boolean Arrow
values and ordinary empty maps in DAG-CBOR. Loading a stored array builds its table straight from the stored data (`array_from_canonical_data`, used
by `Array`'s structure hook). That decoder
checks every key and value to be exactly of the type's Python types before pyarrow sees it,
since pyarrow would otherwise coerce an int into a float or text into bytes, or ignore an extra
struct field; it also refuses a non-canonical type and rows out of order or repeated.
Arrow-to-Python decoding converts scalar columns and non-null struct children in
batches of at most 4,096 rows. List elements are decoded from their visible flattened span;
nullable values are compacted before decoding and restored to their row positions afterwards,
so hidden child slots are ignored. Nested Arrow normalization restores declared field metadata
without treating null child slots under null parents as visible non-nullable values.

## Running queries in DuckDB

`DuckDBArrowExecutor` (`duckdb_engine/executor.py`) does SQL execution, normalization, result
validation, and resource limits for every query, local ones and a workflow's query nodes alike,
and native cancellation for local ones (a query node runs on the calling thread, with no event loop
to cancel it). It binds each input's Arrow table, which the array converts once and keeps; DuckDB
output is read in bounded Arrow batches and validated, and the result is an `Array` holding that
table, whose Python rows are decoded only if something reads them.

The plan nodes are in `entity_defns/query_plan.py` (`QuerySource`, `QueryFilter`, `QuerySelect`,
`QuerySelectValue`, `QuerySel`, `QueryRename`, `QueryRetype`, `QueryLimit`, `QueryAggregate`,
`QueryJoin`, `QuerySQL`). `duckdb_engine/compiler.py` compiles a plan's full expression tree into
one parameterized DuckDB query, never collecting or evaluating intermediate Python values, and
preserves SQL null behavior inside expressions while turning a null top-level filter predicate
into a non-match. The query builder and `fxtr.sql` are two ways to write the same kind of query:
the builder's operations are data-only plan nodes whose result type is derived from the input
types, and `QuerySQL` is the plan node carrying a user SELECT and a declared result type, checked
against its bound result and complete Arrow values. Either compiles, with its named lazy inputs,
into one DuckDB statement and runs on the same executor. A workflow's query node carries its plan
in the canonical encoding of `runner/query_codec.py` and is compiled on the host
(`../runner/CODE_OVERVIEW.md`, "Query nodes").

## SQL

`fxtr.sql` (`array_sql.py`) is the one SQL entry point: `tables` names every lazy input, all of
them concrete arrays and local queries (it returns an `ArrayQuery` with its own executor) or all
handles and handle queries from one workflow attempt (it returns an `ArrayHandleQuery`), and its
result composes with query-builder queries and other SQL. Each SQL input separates a `dims` struct
from its `value`. Output SQL returns `dims` and `value` under `output_type`, which defaults to the
type of a single table and is required with more; dimensionless results need only `value`. SQL
input CTEs bind in one native query. `entity_defns/sql.py` parses one SELECT and binds positional
and named parameters with DuckDB's parser and tokenizer, without changing literal strings or
comments. Binding checks names and struct fields before flattening; Arrow validates values,
nullability, unique dimension keys, and cardinality before collection or storage.

User SQL reads only the tables it was given by name. `validate_sql_reads` in `entity_defns/sql.py`
walks DuckDB's parse tree of the statement: every table read is a named input or one of the
statement's `WITH` queries, with no schema or catalog qualifier, and the only table functions are
`unnest`, `range`, and `generate_series`, so SQL over a filtered input cannot name the registered
source table beneath it. `QuerySQL` runs that check when a plan is built and when one is decoded.
Source tables are registered under names with a random part that changes on every execution, and
those names are removed from DuckDB's error text. Every host runs user SQL; there is no setting to
enable it. The parse-tree check is what bounds a statement, and it bounds it within the array
model: a host whose SQL authors do not trust each other needs OS/container isolation, which no
DuckDB setting replaces.

SQL sees an entity ID as its CID bytes, and an ID bound as a parameter is bound as those bytes. An
ID column of a result must hold well-formed DAG-CBOR SHA-256 CIDs, and SQL can write any such CID;
an ID may be declared as another
entity type, as `EntityID.bind_type` allows in Python. Storing a result refuses a reference to an
entity the storage does not have (`UnknownReferenceError`). In the query builder, an entity ID may
be a literal anywhere, as a `$` parameter may be in SQL, under the same rule. Literal values are
checked against their declared schemas at construction, so ordinary bytes cannot claim to be an ID.

## Limits and cancellation

Each DuckDB connection is in-memory, with file and network access and extension auto-install and
loading disabled, and a locked configuration. `DuckDBExecutionLimits` leaves memory and threads at
DuckDB's native defaults and adds no deadline or result-size cap unless configured, and there is no
query concurrency cap by default; native runs still use AnyIO's worker-thread capacity. A
configured deadline runs from waiting for query capacity, through converting the inputs to Arrow,
to handling the result; the error says whether the time ran out while the query was still waiting
for capacity. A configured byte limit applies to the Arrow output. DuckDB's memory setting does not
cover all Python and Arrow allocations and so cannot bound total process memory. A query deep
enough to exhaust Python's recursion limit fails in the codec or the compiler as an actionable
`QueryError`. Cancelling the AnyIO call interrupts the DuckDB connection and keeps its capacity
slot until the native thread exits.

## Where to change things

- **A new query operation:** its expression or plan node and type rule (`query.py`,
  `query_plan.py`), its compilation (`duckdb_engine/compiler.py`), its encoding for query nodes
  (`runner/query_codec.py`), and the methods that build it on `Array`, `ArrayQuery`
  (`duckdb_engine/local_query.py`), and `ArrayHandleQuery` (`experiment/handles.py`).
- **A new value shape:** `schema.py` (the constructors, the annotation grammar, coercion,
  inference, assignability) and its Arrow layout and validation in `arrow.py`.
- **How a class serializes:** its own serialization hooks (the protocol in `dataclass_bases.py`),
  not the converter.

## Tests

- `tests/test_dataclass_entities.py`: dataclass entities, tagged subparts, union refs, registry
  integration, strictness, the hook protocol, the load-back check on serialize.
- `tests/test_dynamic_data.py`: `DynamicData`: construction, `as_type` / `try_as_type`, the
  transparent wire form, store round trips, equality.
- `tests/test_dataclass_quoted_annotations.py`: quoted names nested in field annotations, for
  entities and plain dataclasses, run against twin classes with and without the future import
  (the ones under `from __future__ import annotations` are in `future_annotations_testing.py`).
- `tests/test_schema.py`: schema constructors, the annotation grammar, presentation, wire form,
  coercion, inference, assignability.
- `tests/test_array.py`: dims and array types, partial array types and `expect_type`, array
  construction and access, `sel` / `field` / `combine` / `stack`, `join_keys`, serialization round
  trips.
- `tests/test_arrow_arrays.py`: Arrow schema mapping, validation, and canonical CBOR identity, over
  `tests/array_testing.py`'s arrays covering every value shape.
- `tests/test_dual_backed_array.py`: an array built from Arrow against the same one built from
  rows: reads, equality, hashing, copies, pickling, every operation, content IDs, storage,
  canonical decoding, the wire, and what each step decodes.
- `tests/test_query.py`: typed predicates and expressions, query type derivation, concrete array
  execution, and canonical identity of query results.
- `tests/test_local_array_queries.py`, `test_local_query_connections.py`,
  `test_duckdb_executor.py`: lazy local queries, identity-independent Arrow caching, concurrency,
  limits, cancellation, and traced-ID preservation; reusable connection cleanup, isolation, and
  stale interrupt callbacks; DuckDB execution and automatic Arrow-result preservation.
- `tests/test_query_semantics.py`: answers that Python, DuckDB, and the wire could each get
  differently: names differing by case, integer division and modulo, tie limits, `group_by`
  order, and what a step receives and catches.
- `tests/test_query_limits.py`, `test_array_query_boundaries.py`,
  `test_postgres_array_query_boundaries.py`: native defaults, opt-in limits, a query node's single
  DuckDB thread, and recursion errors; empty structs, ID literals, deadlines, canonical byte
  limits, shared capacity, and asynchronous Arrow collection; entity references in results read
  back from reopened storage, against real PostgreSQL.
- `tests/test_query_entity_integrity.py`: malformed literals, opaque entity columns, the
  references a result keeps, and PostgreSQL query results.
- `tests/test_deep_array_queries.py`: deep nullable structs and lists, canonical Arrow round
  trips, nested entity integrity, and native error recovery, over `tests/deep_array_testing.py`'s
  arrays nested many levels deep.
- `tests/test_user_array_sql.py`, `test_user_array_sql_adversarial.py`: parser-based bindings,
  named lazy inputs, nested SQL results, entity IDs in SQL, recovery, and native controls;
  injection-shaped names, recursion, windows, set operations, duplicate keys across batches,
  timeouts, and concurrent collectors.
