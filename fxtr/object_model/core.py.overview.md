# core.py Overview

This file (~5865 lines) defines the core object representation for the fxtr hub entity system, built on the IPLD data model and DAG-CBOR codec. It provides the foundation for defining, serializing, and traversing typed immutable data structures.

## Key Concepts

- **Values**: Immutable objects that can be serialized to/from IPLD
- **TypeSpecs**: Objects describing a set of values and how to serialize them
- **Entities**: Self-describing values with content-addressed CIDs; always include their own type-spec when serialized

## File Structure (by line ranges)

| Lines | Region | Description |
|-------|--------|-------------|
| 1-100 | Header/Imports | Module docstring explaining the type system, imports |
| 100-180 | Protocol definitions | `EntityCreationState`, `HubValueVisitor`, `RecursiveEntityVisitor`, `EntityLookup` |
| 180-235 | Helper types | `EntityForwardRefMetadata`, `HasTypeSpec`, `CID_Of` |
| 235-365 | Entity base | `EntityBase`, `AnyFrozenIPLDable`, `freeze()` |
| 365-520 | TypeSpec base classes | `TypeSpecBase`, `TypeSpecBase_SomeEntity` |
| 520-650 | Primitive specs | `TypeSpec_Primitive`, `TypeSpec_ExternalCID` |
| 650-1280 | Collection specs | Fixed/variable tuples, frozendicts (string/entity/CID keys), frozensets |
| 1280-1680 | Variant/union specs | `TypeSpec_Optional`, `TypeSpec_UnionOfEntities`, `TypeSpec_AnyEntity`, `TypeSpec_AnyExternalServiceIdentifier` |
| 1680-1860 | Builtin entity helpers | `TypeSpecBase_SomeBuiltinEntity`, `BuiltinEntityBase`, `BuiltinEntityFromAnnotations` |
| 1860-2550 | Data entities | `DataEntityBase`, `DataEntitySchema`, `TypeSpec_DataEntity`, `TypeSpec_DataEntityForwardRef` |
| 2550-3830 | Interfaces | `InterfaceDefn`, `InterfaceSpec`, `FxtrCallTarget`, `ExternalServiceIdentifier`, `TypeSpec_Plan` |
| 3830-4050 | AnyFrozenIPLDable spec | `TypeSpec_AnyFrozenIPLDable` |
| 4050-4195 | Type registry | `HUB_TYPE_SPEC_REGISTRY`, `TypeSpec_TypeSpec`, `ipld_to_type_spec()`, `ipld_entity_data_to_entity()` |
| 4195-4600 | Type conversion | `python_type_to_hub_type_spec()`, `hub_type_spec_to_python_type()` |
| 4600-5160 | Tasks and runs | `Task`, `TaskFunction`, `Workflow`, `WorkflowFunction`, `Run`, `RunSet`, handles |
| 5160-5595 | Runtime config | `RuntimeEnvRetriever`, `RuntimeEnvConfigAssignment`, `SecretRetriever`, `RuntimeCoordinatorBinding` |
| 5595-5865 | Access paths | `AccessPathStep_*` classes for traversing entity structures |

---

## Major Types

### Base Classes

**`EntityBase`** (line ~238)
- Abstract base class for all entities
- Has `_fxtr_cid` (content address) and `_fxtr_typespec` attributes
- Equality/hash based on CID

**`TypeSpecBase`** (line ~370)
- Abstract base class for all type specifications
- Defines: `validate()`, `traverse_value()`, `value_to_ipld()`, `ipld_to_value()`
- Also defines self-serialization: `traverse_self()`, `self_to_ipld()`, `ipld_to_this_type()`

**`TypeSpecBase_SomeEntity`** (line ~470)
- Base for type specs describing entity types
- Adds `entity_data_to_ipld()`, `ipld_to_entity_object()`
- Entity values serialize as CID links in compound structures

### Primitive Type Specs

**`TypeSpec_Primitive`** (line ~527)
- Handles `int`, `float`, `str`, `bool`, `None`
- Serializes primitives directly to IPLD

**`TypeSpec_ExternalCID`** (line ~595)
- For CID values referencing objects not currently present
- Serializes as base32-encoded strings (not as IPLD links)

### Collection Type Specs

**`TypeSpec_FixedSizeTuple`** (line ~654) - Fixed-size tuples with specific element types

**`TypeSpec_VariableSizeTuple`** (line ~758) - Homogeneous variable-length tuples

**`TypeSpec_FrozenDictStringKey`** (line ~843) - String-keyed frozendicts

**`TypeSpec_FrozenDictEntityKey`** (line ~926) - Entity-keyed frozendicts, stored as sorted `[key_link, value]` pairs

**`TypeSpec_FrozenDictCIDKey`** (line ~1073) - CID-keyed frozendicts, keys stored as base32 strings

**`TypeSpec_FrozenSetOfEntities`** (line ~1178) - Sets of entities, stored as sorted CID lists

### Union/Optional Type Specs

**`TypeSpec_Optional`** (line ~1289) - `T | None`

**`TypeSpec_UnionOfEntities`** (line ~1364) - Union of known entity types

**`TypeSpec_AnyEntity`** (line ~1507) - Accepts any entity type

**`TypeSpec_AnyExternalServiceIdentifier`** (line ~1589) - Any service identifier

### Builtin Entity System

**`BuiltinEntityBase`** (line ~1727)
- Base for entities with hardcoded schemas (not user-defined)
- Automatically creates a `typespec_cls` for each subclass via `typespec_kind` kwarg

**`BuiltinEntityFromAnnotations`** (line ~1782)
- Helper for builtin entities that infer schema from type annotations
- Used with `@dataclasses.dataclass(frozen=True)`

### User-Defined Entities

**`DataEntityBase`** (line ~1988)
- Base for user-defined data entities (frozen dataclasses)
- Uses `UserDefinedEntityMeta` metaclass for type equality based on schema
- Keyword args: `type_id`, `namespace_id`, `schema_forward_defns`

**`DataEntitySchema`** (line ~2193)
- Entity that defines the schema of a data entity
- Fields: `namespace_id`, `type_id`, `field_type_specs`

**`TypeSpec_DataEntity`** (line ~2270)
- TypeSpec for user-defined data entities
- Contains a `DataEntitySchema` and weak reference to Python class
- Use `TypeSpec_DataEntity.build()` to construct

**`TypeSpec_DataEntityForwardRef`** (line ~2452)
- Forward reference to a data entity by `namespace_id`/`type_id`
- Used for recursive type definitions

### Interfaces and Call Targets

**`InterfaceDefn`** (line ~2556)
- Definition of an interface (methods, type vars, statelessness)
- Fields: `namespace_id`, `interface_id`, `methods`, `type_vars`, `stateless`

**`InterfaceSpec`** (line ~2607)
- Concrete interface specification with type variables bound
- Use `.define_external_service_impl()` to create `ExternalServiceIdentifier`

**`FxtrCallTarget`** (line ~3004)
- Base class for callable types (abstract interfaces and concrete implementations)
- Abstract call targets: set `interface_id`, all methods must be async+abstract
- Concrete implementations: implement `interface_spec()` method

**`ExternalServiceIdentifier`** (line ~3592)
- Identifier for an external service implementing an interface
- Fields: `namespace_id`, `service_id`, `interface_spec`

**`TypeSpec_Plan`** (line ~3649)
- TypeSpec for interface implementation plans
- Plans can be `ExternalServiceIdentifier`, `ConfiguredPlan`, or `ComputedPlan`

### Tasks and Workflows

**`TaskFunction[R]`** (line ~4759) - Interface for task functions, has `run_task() -> R`

**`Task[R]`** (line ~4821) - A task entity with `function` (plan) and `tag`

**`TaskResultHandle[R]`** (line ~4779) - Handle to get result of a specific task run

**`WorkflowFunction[S, R]`** (line ~4877) - Interface for workflow steps

**`Workflow[S, R]`** (line ~4904) - Workflow entity with `function` and `tag`

**`WorkflowState[S, R]`** (line ~4978) - Intermediate state with `step_data` and `next` workflow

**`WorkflowResult[R]`** (line ~4994) - Final workflow result

**`Run`** (line ~5058) - Represents a task/workflow run with provenance

### Runtime Configuration

**`RuntimeEnvRetriever[T]`** (line ~5166) - Interface for runtime config (doesn't affect task result)

**`RuntimeEnvConfigAssignment[T]`** (line ~5201) - Assigns a value to a runtime config

**`SecretRetriever[T]`** (line ~5344) - Interface for accessing secrets

**`RuntimeCoordinatorBinding[I]`** (line ~5441) - Binds an implementation to a runtime coordinator

### Type Registry

**`HUB_TYPE_SPEC_REGISTRY`** (line ~4062)
- Maps `$kind` strings to TypeSpec classes
- Used by `ipld_to_type_spec()` to deserialize type specs

**`TypeSpec_TypeSpec`** (line ~4106)
- Meta type spec that describes type specs themselves
- Enables serialization of type specs to IPLD

---

## Key Functions

**`python_type_to_hub_type_spec()`** (line ~4214)
- Converts Python type annotations to hub TypeSpecs
- Handles primitives, tuples, frozendicts, unions, entities, etc.

**`hub_type_spec_to_python_type()`** (line ~4504)
- Converts hub TypeSpecs back to Python type annotations

**`ipld_to_type_spec()`** (line ~4151)
- Deserializes a TypeSpec from IPLD representation

**`ipld_entity_data_to_entity()`** (line ~4165)
- Deserializes an entity from its IPLD data (uses `$typespec` key)

**`define_entity_type()`** (line ~2228)
- Helper to define a data entity type programmatically

**`spec_of()`** (line ~2978)
- Gets InterfaceSpec from a FxtrCallTarget class

**`implements_interface()`** (line ~3544)
- Checks if an implementation matches a target interface type

---

## Access Paths (lines 5595-5865)

Access paths describe how to navigate into entity structures:

- `AccessPathStep_GetAttr` - `.attr_name`
- `AccessPathStep_GetItem_Int` - `[int_key]`
- `AccessPathStep_GetItem_Str` - `[str_key]`
- `AccessPathStep_GetItem_Entity` - `[entity_key]`
- `AccessPathStep_GetItem_CID` - `[cid_key]`
- `AccessPathStep_FrozenDictSortedKey` - key by sorted position
- `AccessPathStep_SetElementSortedOrder` - set element by sorted position
- `AccessPathStep_EntityTypespec` - access `._fxtr_typespec`

---

## Common Patterns

### Defining a Data Entity
```python
class MyEntity(DataEntityBase, type_id="MyEntity^1", namespace_id="my.namespace"):
    """Docstring."""
    field1: str
    field2: int
```

### Defining an Interface
```python
class MyInterface(FxtrCallTarget, interface_id="MyInterface^1", namespace_id="my.namespace", stateless=True):
    @abc.abstractmethod
    async def my_method(self, arg: str) -> int: ...
```

### Creating an External Service
```python
my_service = spec_of(MyInterface).define_external_service_impl(
    service_id="my_service",
    doc="Service description"
)
```

---

## Searching Tips

- **Find a TypeSpec class**: Search for `class TypeSpec_`
- **Find builtin entities**: Search for `typespec_kind="`
- **Find type conversion logic**: Look in `python_type_to_hub_type_spec` or `hub_type_spec_to_python_type`
- **Find IPLD serialization**: Look for `self_to_ipld`, `value_to_ipld`, `entity_data_to_ipld`
- **Find deserialization**: Look for `ipld_to_this_type`, `ipld_to_value`, `ipld_to_entity_object`
- **Find validation**: Look for `validate` methods
- **Find traversal**: Look for `traverse_value`, `traverse_self`
