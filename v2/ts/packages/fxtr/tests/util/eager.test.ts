import { describe, expect, it } from "vitest";
import { Eager } from "../../src/util/eager.js";

// ---------------------------------------------------------------------------
// Eager.resolve / Eager.resolveSync / Eager.reject
// ---------------------------------------------------------------------------

describe("Eager constructors", () => {
	it("Eager.resolve wraps a plain value in Eager", () => {
		const e = Eager.resolve(42);
		expect(e).toBeInstanceOf(Eager);
		expect(e.unwrap()).toBe(42);
	});

	it("Eager.resolve passes through an Eager", () => {
		const original = Eager.resolve("hello");
		const result = Eager.resolve(original);
		expect(result).toBe(original);
	});

	it("Eager.resolve passes through a Promise", () => {
		const p = Promise.resolve(10);
		const result = Eager.resolve(p);
		expect(result).toBe(p);
	});

	it("Eager.resolve passes through a custom thenable", () => {
		// biome-ignore lint/suspicious/noThenProperty: testing thenable detection
		const thenable = { then: (cb: (v: number) => void) => cb(99) };
		const result = Eager.resolve(thenable);
		// Should return the thenable itself, not wrap it
		expect(result).toBe(thenable);
	});

	it("Eager.resolveSync wraps a plain value", () => {
		const e = Eager.resolveSync(42);
		expect(e).toBeInstanceOf(Eager);
		expect(e.unwrap()).toBe(42);
	});

	it("Eager.resolveSync passes through an Eager", () => {
		const original = Eager.resolve("test") as Eager<string>;
		const result = Eager.resolveSync(original);
		expect(result).toBe(original);
	});

	it("Eager.resolveSync throws on a Promise", () => {
		// biome-ignore lint/suspicious/noExplicitAny: testing runtime rejection of wrong types
		expect(() => Eager.resolveSync(Promise.resolve(1) as any)).toThrow(
			"Eager.resolveSync() received a thenable",
		);
	});

	it("Eager.reject creates an error Eager", () => {
		const e = Eager.reject(new Error("boom"));
		expect(e).toBeInstanceOf(Eager);
		expect(() => e.unwrap()).toThrow("boom");
	});
});

// ---------------------------------------------------------------------------
// .unwrap()
// ---------------------------------------------------------------------------

describe("Eager.unwrap", () => {
	it("returns the value for resolved Eager", () => {
		expect(Eager.resolve(42).unwrap()).toBe(42);
	});

	it("throws the error for rejected Eager", () => {
		const err = new Error("test");
		expect(() => Eager.reject(err).unwrap()).toThrow(err);
	});

	it("can unwrap undefined", () => {
		expect(Eager.resolve(undefined).unwrap()).toBeUndefined();
	});

	it("can unwrap null", () => {
		expect(Eager.resolve(null).unwrap()).toBeNull();
	});
});

// ---------------------------------------------------------------------------
// .then()
// ---------------------------------------------------------------------------

describe("Eager.then", () => {
	it("calls onFulfilled synchronously for resolved Eager", () => {
		const result = Eager.resolve(5).then((v) => v * 2);
		expect(result).toBeInstanceOf(Eager);
		expect((result as Eager<number>).unwrap()).toBe(10);
	});

	it("skips onFulfilled for rejected Eager", () => {
		const err = new Error("fail");
		const result = Eager.reject(err).then((v) => v);
		expect(result).toBeInstanceOf(Eager);
		expect(() => (result as Eager<never>).unwrap()).toThrow(err);
	});

	it("calls onRejected for rejected Eager", () => {
		const result = Eager.reject(new Error("fail")).then(undefined, (e) => (e as Error).message);
		expect(result).toBeInstanceOf(Eager);
		expect((result as Eager<string>).unwrap()).toBe("fail");
	});

	it("wraps thrown exceptions in error Eager", () => {
		const result = Eager.resolve(1).then(() => {
			throw new Error("kaboom");
		});
		expect(result).toBeInstanceOf(Eager);
		expect(() => (result as Eager<never>).unwrap()).toThrow("kaboom");
	});

	it("returns Eager when callback returns Eager", () => {
		const result = Eager.resolve(3).then((v) => Eager.resolve(v + 1));
		expect(result).toBeInstanceOf(Eager);
		expect((result as Eager<number>).unwrap()).toBe(4);
	});

	it("returns Promise when callback returns Promise", async () => {
		const result = Eager.resolve(3).then((v) => Promise.resolve(v + 1));
		expect(result).toBeInstanceOf(Promise);
		expect(await result).toBe(4);
	});

	it("passes through value when onFulfilled is null", () => {
		const result = Eager.resolve(42).then(null);
		expect(result).toBeInstanceOf(Eager);
		expect((result as Eager<number>).unwrap()).toBe(42);
	});

	it("propagates error when onRejected is null", () => {
		const err = new Error("oops");
		const result = Eager.reject(err).then((v) => v, null);
		expect(result).toBeInstanceOf(Eager);
		expect(() => (result as Eager<never>).unwrap()).toThrow(err);
	});

	it("wraps exceptions thrown by onRejected", () => {
		const result = Eager.reject(new Error("first")).then(undefined, () => {
			throw new Error("second");
		});
		expect(result).toBeInstanceOf(Eager);
		expect(() => (result as Eager<never>).unwrap()).toThrow("second");
	});

	it("chains multiple .then() calls synchronously", () => {
		const result = Eager.resolve(1)
			.then((v) => v + 1)
			.then((v) => (v as number) * 3)
			.then((v) => `result: ${v}`);
		expect(result).toBeInstanceOf(Eager);
		expect((result as Eager<string>).unwrap()).toBe("result: 6");
	});

	it("chain becomes Promise as soon as one step returns Promise", async () => {
		const result = Eager.resolve(1)
			.then((v) => v + 1)
			.then((v) => Promise.resolve((v as number) * 3));
		expect(result).toBeInstanceOf(Promise);
		expect(await result).toBe(6);
	});
});

// ---------------------------------------------------------------------------
// .catch()
// ---------------------------------------------------------------------------

describe("Eager.catch", () => {
	it("catches error and returns Eager", () => {
		const result = Eager.reject(new Error("fail")).catch((e) => (e as Error).message);
		expect(result).toBeInstanceOf(Eager);
		expect((result as Eager<string>).unwrap()).toBe("fail");
	});

	it("does not affect resolved Eager", () => {
		const result = Eager.resolve(42).catch(() => -1);
		expect(result).toBeInstanceOf(Eager);
		expect((result as Eager<number>).unwrap()).toBe(42);
	});
});

// ---------------------------------------------------------------------------
// .finally()
// ---------------------------------------------------------------------------

describe("Eager.finally", () => {
	it("calls onFinally for resolved Eager and preserves value", () => {
		let called = false;
		const result = Eager.resolve(42).finally(() => {
			called = true;
		});
		expect(called).toBe(true);
		expect(result).toBeInstanceOf(Eager);
		expect((result as Eager<number>).unwrap()).toBe(42);
	});

	it("calls onFinally for rejected Eager and preserves error", () => {
		let called = false;
		const err = new Error("fail");
		const result = Eager.reject(err).finally(() => {
			called = true;
		});
		expect(called).toBe(true);
		expect(result).toBeInstanceOf(Eager);
		expect(() => (result as Eager<never>).unwrap()).toThrow(err);
	});
});

// ---------------------------------------------------------------------------
// await (thenable protocol)
// ---------------------------------------------------------------------------

describe("Eager as thenable", () => {
	it("can be awaited for resolved value", async () => {
		const result = await Eager.resolve(42);
		expect(result).toBe(42);
	});

	it("can be awaited for rejected error", async () => {
		await expect(Eager.reject(new Error("boom"))).rejects.toThrow("boom");
	});

	it("works with Promise.all", async () => {
		const result = await Promise.all([Eager.resolve(1), Eager.resolve(2), Promise.resolve(3)]);
		expect(result).toEqual([1, 2, 3]);
	});
});

// ---------------------------------------------------------------------------
// Eager.all()
// ---------------------------------------------------------------------------

describe("Eager.all", () => {
	it("returns Eager when all inputs are Eager", () => {
		const result = Eager.all([Eager.resolve(1), Eager.resolve(2), Eager.resolve(3)]);
		expect(result).toBeInstanceOf(Eager);
		expect((result as Eager<number[]>).unwrap()).toEqual([1, 2, 3]);
	});

	it("returns Promise when any input is a Promise", async () => {
		const result = Eager.all([Eager.resolve(1), Promise.resolve(2), Eager.resolve(3)]);
		expect(result).toBeInstanceOf(Promise);
		expect(await result).toEqual([1, 2, 3]);
	});

	it("returns Eager for empty array", () => {
		const result = Eager.all([]);
		expect(result).toBeInstanceOf(Eager);
		expect((result as Eager<[]>).unwrap()).toEqual([]);
	});

	it("fast-fails on first error Eager", () => {
		const err = new Error("fail");
		const result = Eager.all([Eager.resolve(1), Eager.reject(err), Eager.resolve(3)]);
		expect(result).toBeInstanceOf(Eager);
		expect(() => (result as Eager<never>).unwrap()).toThrow(err);
	});

	it("delegates to Promise.all when a Promise comes before an error Eager", async () => {
		const err = new Error("fail");
		const result = Eager.all([Promise.resolve(1), Eager.reject(err)]);
		expect(result).toBeInstanceOf(Promise);
		await expect(result).rejects.toThrow(err);
	});

	it("preserves tuple types", () => {
		const result = Eager.all([Eager.resolve("hello"), Eager.resolve(42), Eager.resolve(true)]);
		expect(result).toBeInstanceOf(Eager);
		const values = (result as Eager<[string, number, boolean]>).unwrap();
		expect(values).toEqual(["hello", 42, true]);
	});
});

// ---------------------------------------------------------------------------
// Integration: mixed chains
// ---------------------------------------------------------------------------

describe("mixed Eager/Promise chains", () => {
	it("Eager→Eager→Eager stays fully sync", () => {
		const result = Eager.resolve(1)
			.then((v) => Eager.resolve(v + 1))
			.then((v) => Eager.resolve((v as number) * 2));
		expect(result).toBeInstanceOf(Eager);
		expect((result as Eager<number>).unwrap()).toBe(4);
	});

	it("Eager→Promise→anything becomes Promise", async () => {
		const result = Eager.resolve(1)
			.then((v) => Promise.resolve(v + 1))
			// After the promise, we're in Promise-land
			.then((v) => v * 2);
		expect(result).toBeInstanceOf(Promise);
		expect(await result).toBe(4);
	});

	it("error in Eager chain propagates through Eager", () => {
		const result = Eager.resolve(1)
			.then((): number => {
				throw new Error("mid-chain");
			})
			.then((v) => v * 2)
			.catch((e) => `caught: ${(e as Error).message}`);
		expect(result).toBeInstanceOf(Eager);
		expect((result as Eager<string>).unwrap()).toBe("caught: mid-chain");
	});

	it("Eager.all with all Eager results chains sync", () => {
		const values = [1, 2, 3].map((v) => Eager.resolve(v));
		const result = Eager.all(values).then((arr) =>
			Eager.resolve((arr as number[]).reduce((a, b) => a + b, 0)),
		);
		expect(result).toBeInstanceOf(Eager);
		expect((result as Eager<number>).unwrap()).toBe(6);
	});
});
