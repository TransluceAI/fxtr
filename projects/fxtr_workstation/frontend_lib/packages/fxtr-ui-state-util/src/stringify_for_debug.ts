/**
 * `JSON.stringify` wrapper for session-state inspection.
 *
 * Stub classes carry session backrefs that produce circular
 * structures when fed to vanilla `JSON.stringify` —
 * `stub → session → updateCtx → session` is the typical loop. The
 * stub classes in `@fxtr-ui/core` (and their connector-side
 * concrete subclasses) define a `debugSummary(): string` method
 * that returns a short tagged form; the replacer below detects
 * that method by duck typing and calls it, flattening the stub
 * at the boundary so the rest of the structured value can
 * serialize normally.
 *
 * Use this whenever you need a human-readable dump of session
 * user state, a load/invoke result, an in-flight slot fill, etc.
 * for debugging UIs or log lines.
 *
 * Not intended for protocol-bearing serialization — the connector
 * packages own their own wire codecs (`escapeWire` and friends),
 * which encode stubs as tagged JSON arrays.
 *
 * The duck-typed dispatch means any value the consumer wants to
 * flatten can opt in by defining `debugSummary(): string` — no
 * registration step, no instanceof checks here. (Existing stub
 * classes already define it; future stub kinds inherit it from
 * the abstract base.)
 */

/**
 * `JSON.stringify` replacer that swaps any object with a
 * `debugSummary(): string` method for its summary string.
 * Suitable as a third argument to `JSON.stringify(value,
 * debugReplacer, indent?)`; also re-used by `stringifyForDebug`
 * below.
 *
 * Exported so consumers who want to compose with their own
 * replacer logic (e.g. eliding additional types) can chain off it.
 */
export function debugReplacer(_key: string, value: unknown): unknown {
	if (
		value !== null &&
		typeof value === "object" &&
		"debugSummary" in value &&
		typeof (value as { debugSummary: unknown }).debugSummary === "function"
	) {
		return (value as { debugSummary: () => string }).debugSummary();
	}
	return value;
}

/**
 * `JSON.stringify` wrapper that summarizes session-state stubs.
 * Returns a string suitable for inspection / debug display.
 *
 * `indent` is forwarded to `JSON.stringify` — pass `2` for
 * pretty-printed multi-line output, omit for a compact single-line
 * form.
 */
export function stringifyForDebug(value: unknown, indent?: number): string {
	return JSON.stringify(value, debugReplacer, indent);
}
