# Scope Discipline

FxtrScopes are used to maintain the closed-subgraph invariant in fxtr. In particular, each new
external resource creates a new scope, and entities exposed by that resource are lazily-loadable
inside that scope, but must be _copied_ if we want to access them outside the scope. Additionally,
scopes can be used to define temporary entities that should be garbage-collected at the end of the
scope unless they are explicitly copied out or stored elsewhere.

To make this easy in common cases, there is a global contextvar called the "active scope", and
entities are stored and loaded into this by default. New scopes inherit from the active scope by
default, so that context manager blocks naturally track with Python scopes.

However, this implicit scope management can lead to issues when dealing with long-lived scopes,
interactions between scopes, and stateful library code. Thus, when implementing _library_ code in
fxtr, it is useful to enforce stronger programming patterns that are more verbose but obviously
correct via local reasoning. This document describes these patterns, known as "scope discipline".
Library code should generally follow these principles.

## Don't Use Implicit Active Scope
Under scope discipline, code should _never_ implicitly use the active scope. This means no bare
calls to `ref.load()` or `entity.store()`. Instead, an explicit scope should always be passed,
e.g. `ref.load(scope)`, or `entity.store(scope)`.

This also means that _every_ scope should be created as `FxtrScope(isolated=True)`. If a scope needs
to depend on a parent scope, this should be explicitly marked as an `include`.

## Avoid Active Scope Except At User Boundaries
Under scope discipline, code should never use the active scope context to manage storage of entities
owned by that code (via e.g. `FxtrScope.get_active()` or other methods). Instead, all loading and
storing should occur into a scope that is explicitly passed as an argument or stored as an
attribute. This makes sure that the scope lifetimes are appropriately threaded through, and the
flow of the scope is obvious from the call site, rather than being implicitly managed by the
context manager.

There are three specific cases where the active scope can be used under scope discipline:
- In a function that is intended to be directly called by users, the active scope can be retrieved
    at the start of the function, and used to load, store, or interpret the data that is directly
    received from or passed to the user. In this case, treat it as if the user had explicitly
    provided the scope. Usually, as a best practice, you should also allow the user to provide a
    `scope` (or `from_scope`/`into_scope` etc) keyword which will override this behavior.
- When invoking user-provided code, if that user-provided code expects a particular scope to be
    active, you can use `with scope.as_active()` right around that call to set the active scope to
    some existing scope.
- When implementing a method on a service impl method (e.g. a `@service_method`-decorated function
    on a `TypedServiceImpl`), the active scope may be captured via `get_active()` and manipulated,
    since it is the scope that contains the arguments and must be used to store/mount the results.
    In this case you can treat it as if the active scope has been explicitly passed by the
    framework.

## Avoid Capturing Ambient Scope Context In Tasks

Every `tg.start` / `tg.start_soon` copies the calling task's context — including the
active-scope contextvar. The new task then carries that snapshot for its whole life, even
after the context it came from has ended. This matters most when a long-lived task is
started from a context you don't control: above all a service dispatch, whose ambient
scope is the ephemeral per-call scope. Any non-isolated scope created downstream in such
a task (including inside library code it calls) silently adopts that ephemeral scope as a
structural parent, which then cannot end while the new scope lives — a teardown deadlock
whose tasks all look idle.

Under scope discipline, a task that outlives the context it was started from should not
rely on its inherited active scope. Either create only isolated scopes (and call only
code that does the same), or install a context you control at the top of the task: capture
the active scope at a known point (e.g. construction of the object that owns the task) and
set it back at task entry. fxtr2 packages this idiom as
`fxtr2.common.task_utils.start_with_scope` / `start_soon_with_scope`, which start a task
with a chosen scope installed — or, with `scope=None`, with *no* active scope, so any
implicit use anywhere in the task raises `NoActiveScopeError` immediately instead of
silently binding to the inherited snapshot (`no_active_scope()` is the block form of the
same thing).

Be especially careful when such a task runs **user-defined code** (callbacks, handlers,
entrypoints): user code is entitled to use the active scope, so even a library that never
touches ambient state itself will silently hand user code a stale forked context. Before
invoking user code from a task like this, install the scope that user code should see
(`with scope.as_active():`, per the user-boundary cases above) rather than letting the
inherited snapshot leak through.

When a hang of this class does occur, the scope-drain report
(`fxtr2/debug/scope_report.py`) names the offending edge — it classifies every implicit
active-scope capture as same-task or fork-inherited.

## Pass And Store Scopes Alongside References
Any time an `EntityRef` or `FxtrID` is passed as an argument _or_ stored as an attribute, you should
always also store a scope where that entity is known to be loadable. This makes it possible to
load the value when it is needed, without relying on implicit scoping rules. The two should always
be stored together, and there should be a comment in the relevant function or class docstring that
explains which references are loadable in which scope. This also applies to `ServiceRef`.

## Document Membership Rationales
Anytime we introduce a claim that a particular entity (or service) is loadable in a given scope,
there should be a comment justifying why. This means:
- When loading an entity from a reference in scope `s`: A comment explaining why we know `s`
    contains the entity (e.g. the caller is required to guarantee this due to the docstring, or we
    just stored it, or we just retrieved it, or we just loaded a parent entity in this scope, etc)
- When calling a function and passing a reference along with a scope, if that function assumes the
    reference is loadable there: A comment explaining why the precondition is satisfied.
- When storing a ref into an attribute: A comment explaining why that ref is loadable in whatever
    scope is stored alongside it.
- (and the same for ServiceRef)

Rationales should usually reference other comments and guarantees provided by other parts of the
library that are being used. E.g. usually the justification for why a given operation is allowed
should refer to docstrings on function arguments or attributes that we have received.

If the rationale seems difficult to determine or the logic does not seem sound, and you are an AI
agent, you should flag it with a TODO and make sure to surface the issue to the user. Do not make
up a rationale or write a "hope" like "this is in the active scope" unless you can point to a
specific other precondition that guarantees this to be true, because then if it fails to work at
runtime it will be very difficult for you to debug! Prefer to write a TODO comment than to
paper over a real bug and confuse yourself later.
