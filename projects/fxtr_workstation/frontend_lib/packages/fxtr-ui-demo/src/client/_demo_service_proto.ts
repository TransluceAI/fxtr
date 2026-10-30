/**
 * TS protocol shape mirroring the demo workstation's Python
 * `_DemoServiceProto`. Used to type the demo's `serviceRef` so
 * `ctx.allocateInvoke(svc, "echo", [42])` checks the method name +
 * args + return type at compile time and the demo doesn't need the
 * `as readonly never[]` cast at the invoke call site.
 *
 * Mirrors `demo_workstation.py`'s `_DemoServiceProto`:
 *
 * - `echo(value)` returns its argument unchanged.
 * - `combine(value, *, name="default")` returns `{value, name}`.
 * - `fail(message)` raises (never returns successfully).
 * - `notify_random(cb)` picks a random int and calls `cb.notify(it)`
 *   once; returns the int sent (or -1 if the callback was closed).
 *
 * Pure type declaration — no runtime metadata. Args / kwargs are
 * `InvokeArgValue`-shaped (the type of `escapeInvokeArgs`'s input);
 * returns are `ResultValue`-shaped (payloads decoded out of the
 * server's invoke result events).
 *
 * Kept in sync with the Python side by hand; future stages could
 * generate this from a shared schema or contract test.
 */

import type { InvokeArgValue, Method, ResultValue } from "@fxtr-ui/core";

export interface _DemoServiceProto {
	readonly echo: Method<readonly [InvokeArgValue], Record<string, never>, ResultValue>;
	readonly combine: Method<
		readonly [InvokeArgValue],
		{ readonly name: string },
		{ readonly value: ResultValue; readonly name: string }
	>;
	readonly fail: Method<readonly [string], Record<string, never>, never>;
	readonly notify_random: Method<readonly [InvokeArgValue], Record<string, never>, number>;
}
