/**
 * Eager<T> — a synchronous thenable.
 *
 * Always deterministically contains either a resolved value or a rejected error.
 * Mirrors Promise semantics exactly, but runs .then() callbacks synchronously
 * instead of scheduling them on the microtask queue.
 *
 * Key properties:
 * - Thenable: `await eager` works (but still yields a microtask — use .unwrap()
 *   or .then() chaining for the sync benefit)
 * - .then(f) returns Eager<U> if f returns Eager<U>, Promise<U> if f returns Promise<U>
 * - .unwrap() returns the value synchronously or throws the error synchronously
 * - Eager.all() returns Eager when all inputs are Eager, Promise otherwise
 */

// ---------------------------------------------------------------------------
// PromiseOrEager<T>
// ---------------------------------------------------------------------------

/** A value that is either synchronously available (Eager) or asynchronously (Promise). */
export type PromiseOrEager<T> = Promise<T> | Eager<T>;

// ---------------------------------------------------------------------------
// Thenable detection helpers
// ---------------------------------------------------------------------------

function isThenable(value: unknown): value is PromiseLike<unknown> {
	return (
		value !== null &&
		value !== undefined &&
		typeof (value as { then?: unknown }).then === "function"
	);
}

// ---------------------------------------------------------------------------
// Eager<T>
// ---------------------------------------------------------------------------

export class Eager<T> {
	/** @internal */ readonly _ok: boolean;
	/** @internal */ readonly _value: T;
	/** @internal */ readonly _error: unknown;

	private constructor(ok: boolean, value: T, error: unknown) {
		this._ok = ok;
		this._value = value;
		this._error = error;
	}

	// -- Constructors --

	/**
	 * Mirrors Promise.resolve:
	 * - Eager<T> → passes through
	 * - Promise/thenable → returns it as-is (can't adopt synchronously)
	 * - plain value → wraps in resolved Eager
	 */
	static resolve<T>(value: Eager<T>): Eager<T>;
	static resolve<T>(value: Promise<T>): Promise<T>;
	static resolve<T>(value: PromiseLike<T>): PromiseLike<T>;
	static resolve<T>(value: T): Eager<T>;
	static resolve<T>(value: T | PromiseLike<T> | Eager<T>): PromiseOrEager<T>;
	static resolve<T>(value: T | PromiseLike<T> | Eager<T>): PromiseOrEager<T> | PromiseLike<T> {
		if (value instanceof Eager) return value;
		if (value instanceof Promise) return value;
		if (isThenable(value)) return value;
		return new Eager<T>(true, value, undefined);
	}

	/**
	 * Always returns Eager<T>. Passes through Eagers, wraps plain values.
	 * Throws if given a Promise or other thenable.
	 */
	static resolveSync<T>(value: T | Eager<T>): Eager<T> {
		if (value instanceof Eager) return value;
		if (isThenable(value)) {
			throw new Error("Eager.resolveSync() received a thenable — use Eager.resolve() instead");
		}
		return new Eager<T>(true, value as T, undefined);
	}

	/** Always returns Eager<never> with the given error. */
	static reject(error: unknown): Eager<never> {
		return new Eager<never>(false, undefined as never, error);
	}

	// -- Instance methods --

	/**
	 * Synchronous .then() — mirrors Promise.then() but calls callbacks immediately.
	 *
	 * Return type depends on what the callback returns:
	 * - Eager<U> → Eager<U>
	 * - Promise<U> → Promise<U>
	 * - plain U → Eager<U>
	 */
	// biome-ignore lint/suspicious/noThenProperty: Eager is a thenable by design
	then<TResult1 = T, TResult2 = never>(
		onFulfilled?: ((value: T) => TResult1 | PromiseLike<TResult1>) | null | undefined,
		onRejected?: ((reason: unknown) => TResult2 | PromiseLike<TResult2>) | null | undefined,
	): PromiseOrEager<TResult1 | TResult2> {
		if (this._ok) {
			if (onFulfilled == null) {
				// No handler: pass value through (like Promise)
				return this as unknown as Eager<TResult1>;
			}
			try {
				const result = onFulfilled(this._value);
				return Eager.resolve(result) as PromiseOrEager<TResult1>;
			} catch (e) {
				return Eager.reject(e);
			}
		}
		// Error state
		if (onRejected == null) {
			// No handler: propagate error (like Promise)
			return this as unknown as Eager<TResult2>;
		}
		try {
			const result = onRejected(this._error);
			return Eager.resolve(result) as PromiseOrEager<TResult2>;
		} catch (e) {
			return Eager.reject(e);
		}
	}

	catch<TResult = never>(
		onRejected: ((reason: unknown) => TResult | PromiseLike<TResult>) | null | undefined,
	): PromiseOrEager<T | TResult> {
		return this.then(undefined, onRejected);
	}

	finally(onFinally: (() => void) | null | undefined): PromiseOrEager<T> {
		return this.then(
			(value) => {
				if (onFinally) onFinally();
				return value;
			},
			(reason) => {
				if (onFinally) onFinally();
				throw reason;
			},
		);
	}

	/** Returns the value synchronously, or throws the error synchronously. */
	unwrap(): T {
		if (this._ok) return this._value;
		throw this._error;
	}

	// -- Static combinators --

	/**
	 * Like Promise.all but returns Eager when all inputs are Eager.
	 * Fast-fails on first error Eager. Falls back to Promise.all on first Promise.
	 */
	static all<T extends readonly unknown[]>(
		values: readonly [...{ [K in keyof T]: PromiseOrEager<T[K]> }],
	): PromiseOrEager<T>;
	static all<T>(values: readonly PromiseOrEager<T>[]): PromiseOrEager<T[]>;
	static all(values: readonly PromiseOrEager<unknown>[]): PromiseOrEager<unknown[]> {
		const results: unknown[] = [];
		for (let i = 0; i < values.length; i++) {
			// biome-ignore lint/style/noNonNullAssertion: bounds checked by loop condition
			const v = values[i]!;
			if (v instanceof Eager) {
				if (!v._ok) return Eager.reject(v._error);
				results.push(v._value);
			} else {
				// Hit a Promise — delegate everything to Promise.all.
				// Eagers are thenables so Promise.all handles them correctly.
				return Promise.all(values) as Promise<unknown[]>;
			}
		}
		return Eager.resolve(results);
	}
}
