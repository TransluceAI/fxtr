/**
 * Tests for @fxtr/react: FxtrProvider, useFxtrLoad, useFxtrScope, FxtrSubscope.
 *
 * @vitest-environment jsdom
 */

import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { Suspense } from "react";
import { afterEach, describe, expect, it } from "vitest";

import {
	type Entity,
	type EntityLoadCodec,
	EntityRef,
	type EntityStoreCodec,
	FXTR_CACHEABLE,
	FXTR_CODEC,
	type FxtrCbor,
	FxtrID,
	type FxtrScope,
	GLOBAL_SCOPE,
	RawFxtrEntityMarkerID,
	RawWeakFxtrID,
	createFxtrScope,
} from "@fxtr/core";

import {
	FxtrProvider,
	FxtrSubscope,
	useFxtrLoad,
	useFxtrLoadQuery,
	useFxtrScope,
} from "../src/index.js";

// Ensure React tree is unmounted after each test so internal scopes
// get scheduleClose'd before we dispose parent scopes.
afterEach(cleanup);

// ---------------------------------------------------------------------------
// Test entity helpers (same pattern as scope.test.ts)
// ---------------------------------------------------------------------------

interface CounterEntity extends Entity {
	readonly count: number;
}

const COUNTER_TYPE_ID = FxtrID.builtin("test-counter-type");

const counterLoadCodec: EntityLoadCodec<CounterEntity> = {
	async supportsEntityType(typeId: FxtrID, _scope: FxtrScope): Promise<boolean> {
		return typeId.key() === COUNTER_TYPE_ID.key();
	},
	async decode(cbor: FxtrCbor, _scope: FxtrScope): Promise<CounterEntity> {
		return makeCounter((cbor as Record<string, unknown>).count as number, true);
	},
};

const counterStoreCodec: EntityStoreCodec<CounterEntity> = {
	async store(value: CounterEntity, scope: FxtrScope): Promise<EntityRef<CounterEntity>> {
		scope.trackBuiltin(COUNTER_TYPE_ID);
		const id = await scope.storeCbor({
			"$fxtr.type": new RawWeakFxtrID(COUNTER_TYPE_ID),
			"$fxtr.ctx": new RawFxtrEntityMarkerID(scope.getContextMarkerId()),
			count: value.count,
		});
		return new EntityRef(id, counterLoadCodec);
	},
};

function makeCounter(count: number, cacheable = false): CounterEntity {
	const obj = { count } as Record<string, unknown>;
	Object.defineProperty(obj, FXTR_CODEC, { value: counterStoreCodec, enumerable: false });
	if (cacheable) {
		Object.defineProperty(obj, FXTR_CACHEABLE, { value: true, enumerable: false });
	}
	return obj as unknown as CounterEntity;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function setupScopeWithLoadedEntity(count: number) {
	const scope = createFxtrScope({ name: "test-root", include: [GLOBAL_SCOPE] });
	const ref = await scope.store(makeCounter(count));
	await scope.load(ref);
	return { scope, ref };
}

async function setupScopeWithEntity(count: number) {
	const scope = createFxtrScope({ name: "test-root", include: [GLOBAL_SCOPE] });
	const ref = await scope.store(makeCounter(count));
	return { scope, ref };
}

// ---------------------------------------------------------------------------
// FxtrProvider
// ---------------------------------------------------------------------------

describe("FxtrProvider", () => {
	it("renders children", async () => {
		const scope = createFxtrScope({ include: [GLOBAL_SCOPE] });
		render(
			<FxtrProvider scope={scope}>
				<div data-testid="child">hello</div>
			</FxtrProvider>,
		);
		expect(screen.getByTestId("child").textContent).toBe("hello");
		cleanup();
		await scope.dispose();
	});
});

// ---------------------------------------------------------------------------
// useFxtrLoad
// ---------------------------------------------------------------------------

describe("useFxtrLoad", () => {
	it("returns cached value synchronously", async () => {
		const { scope, ref } = await setupScopeWithLoadedEntity(42);

		function Counter({ entityRef }: { entityRef: EntityRef<CounterEntity> }) {
			const entity = useFxtrLoad(entityRef);
			return <span data-testid="count">{entity.count}</span>;
		}

		render(
			<FxtrProvider scope={scope}>
				<Suspense fallback={<span data-testid="loading">loading</span>}>
					<Counter entityRef={ref} />
				</Suspense>
			</FxtrProvider>,
		);

		// Should render immediately since entity is pre-cached
		expect(screen.getByTestId("count").textContent).toBe("42");
		cleanup();
		await scope.dispose();
	});

	it("suspends and resolves for uncached entity", async () => {
		const { scope, ref } = await setupScopeWithEntity(99);

		function Counter({ entityRef }: { entityRef: EntityRef<CounterEntity> }) {
			const entity = useFxtrLoad(entityRef);
			return <span data-testid="count">{entity.count}</span>;
		}

		await act(async () => {
			render(
				<FxtrProvider scope={scope}>
					<Suspense fallback={<span data-testid="loading">loading</span>}>
						<Counter entityRef={ref} />
					</Suspense>
				</FxtrProvider>,
			);
		});

		expect(screen.getByTestId("count").textContent).toBe("99");
		cleanup();
		await scope.dispose();
	});

	it("sequential loads in one component work", async () => {
		const scope = createFxtrScope({ name: "test-root", include: [GLOBAL_SCOPE] });
		const ref1 = await scope.store(makeCounter(10));
		const ref2 = await scope.store(makeCounter(20));

		function Both() {
			const e1 = useFxtrLoad(ref1);
			const e2 = useFxtrLoad(ref2);
			return (
				<span data-testid="both">
					{e1.count},{e2.count}
				</span>
			);
		}

		await act(async () => {
			render(
				<FxtrProvider scope={scope}>
					<Suspense fallback={<span>loading</span>}>
						<Both />
					</Suspense>
				</FxtrProvider>,
			);
		});

		expect(screen.getByTestId("both").textContent).toBe("10,20");
		cleanup();
		await scope.dispose();
	});

	it("parent-child cascading loads work via suspense", async () => {
		const scope = createFxtrScope({ name: "test-root", include: [GLOBAL_SCOPE] });
		const innerRef = await scope.store(makeCounter(55));
		const outerRef = await scope.store(makeCounter(77));

		function Inner({ entityRef }: { entityRef: EntityRef<CounterEntity> }) {
			const entity = useFxtrLoad(entityRef);
			return <span data-testid="inner">{entity.count}</span>;
		}

		function Outer({ entityRef }: { entityRef: EntityRef<CounterEntity> }) {
			const entity = useFxtrLoad(entityRef);
			return (
				<div>
					<span data-testid="outer">{entity.count}</span>
					<Suspense fallback={<span>loading inner</span>}>
						<Inner entityRef={innerRef} />
					</Suspense>
				</div>
			);
		}

		await act(async () => {
			render(
				<FxtrProvider scope={scope}>
					<Suspense fallback={<span>loading outer</span>}>
						<Outer entityRef={outerRef} />
					</Suspense>
				</FxtrProvider>,
			);
		});

		expect(screen.getByTestId("outer").textContent).toBe("77");
		expect(screen.getByTestId("inner").textContent).toBe("55");
		cleanup();
		await scope.dispose();
	});
});

// ---------------------------------------------------------------------------
// FxtrSubscope component
// ---------------------------------------------------------------------------

describe("FxtrSubscope component", () => {
	it("children can load entities through subscope boundary", async () => {
		const { scope, ref } = await setupScopeWithLoadedEntity(33);

		function Counter({ entityRef }: { entityRef: EntityRef<CounterEntity> }) {
			const entity = useFxtrLoad(entityRef);
			return <span data-testid="count">{entity.count}</span>;
		}

		await act(async () => {
			render(
				<FxtrProvider scope={scope}>
					<FxtrSubscope>
						<Suspense fallback={<span>loading</span>}>
							<Counter entityRef={ref} />
						</Suspense>
					</FxtrSubscope>
				</FxtrProvider>,
			);
		});

		expect(screen.getByTestId("count").textContent).toBe("33");
		cleanup();
		await scope.dispose();
	});

	it("bridges external scopes via include", async () => {
		const rootScope = createFxtrScope({ name: "test-root", include: [GLOBAL_SCOPE] });
		const externalScope = createFxtrScope({ include: [GLOBAL_SCOPE] });
		const ref = await externalScope.store(makeCounter(99));
		await externalScope.load(ref);

		function Counter({ entityRef }: { entityRef: EntityRef<CounterEntity> }) {
			const entity = useFxtrLoad(entityRef);
			return <span data-testid="count">{entity.count}</span>;
		}

		await act(async () => {
			render(
				<FxtrProvider scope={rootScope}>
					<FxtrSubscope include={[externalScope]}>
						<Suspense fallback={<span>loading</span>}>
							<Counter entityRef={ref} />
						</Suspense>
					</FxtrSubscope>
				</FxtrProvider>,
			);
		});

		expect(screen.getByTestId("count").textContent).toBe("99");
		cleanup();
		await externalScope.dispose();
		await rootScope.dispose();
	});
});

// ---------------------------------------------------------------------------
// useFxtrScope
// ---------------------------------------------------------------------------

describe("useFxtrScope", () => {
	it("returns a mutable scope that can store entities", async () => {
		const rootScope = createFxtrScope({ name: "test-root", include: [GLOBAL_SCOPE] });
		let capturedScope: FxtrScope | null = null;

		function ScopeCapture() {
			const scope = useFxtrScope();
			capturedScope = scope;
			return <span data-testid="captured">ok</span>;
		}

		render(
			<FxtrProvider scope={rootScope}>
				<ScopeCapture />
			</FxtrProvider>,
		);

		expect(screen.getByTestId("captured").textContent).toBe("ok");
		expect(capturedScope).not.toBeNull();
		// biome-ignore lint/style/noNonNullAssertion: asserted above
		const scope = capturedScope!;
		expect(scope.isAlive()).toBe(true);

		const ref = await scope.store(makeCounter(123));
		const loaded = await scope.load(ref);
		expect(loaded.count).toBe(123);

		cleanup();
		await rootScope.dispose();
	});
});

// ---------------------------------------------------------------------------
// useFxtrLoadQuery — ref form
// ---------------------------------------------------------------------------

describe("useFxtrLoadQuery ref form", () => {
	it("returns loading then data", async () => {
		const { scope, ref } = await setupScopeWithEntity(42);
		const snapshots: Array<{ data: number | undefined; loading: boolean; error: unknown }> = [];

		function Status({ entityRef }: { entityRef: EntityRef<CounterEntity> }) {
			const status = useFxtrLoadQuery(entityRef);
			snapshots.push({ data: status.data?.count, loading: status.loading, error: status.error });
			if (status.loading) return <span data-testid="state">loading</span>;
			if (status.error) return <span data-testid="state">error</span>;
			return <span data-testid="state">{status.data?.count}</span>;
		}

		render(
			<FxtrProvider scope={scope}>
				<Status entityRef={ref} />
			</FxtrProvider>,
		);

		// Initial render should be loading
		expect(snapshots[0]?.loading).toBe(true);
		expect(snapshots[0]?.data).toBeUndefined();

		await waitFor(() => {
			expect(screen.getByTestId("state").textContent).toBe("42");
		});

		// Should have a settled snapshot
		// biome-ignore lint/style/noNonNullAssertion: snapshots is non-empty after waitFor
		const last = snapshots.at(-1)!;
		expect(last.loading).toBe(false);
		expect(last.data).toBe(42);
		expect(last.error).toBeUndefined();

		cleanup();
		await scope.dispose();
	});

	it("returns cached value synchronously", async () => {
		const { scope, ref } = await setupScopeWithLoadedEntity(99);
		const snapshots: Array<{ data: number | undefined; loading: boolean }> = [];

		function Status({ entityRef }: { entityRef: EntityRef<CounterEntity> }) {
			const status = useFxtrLoadQuery(entityRef);
			snapshots.push({ data: status.data?.count, loading: status.loading });
			return <span data-testid="state">{status.data?.count ?? "none"}</span>;
		}

		render(
			<FxtrProvider scope={scope}>
				<Status entityRef={ref} />
			</FxtrProvider>,
		);

		// Should render with data immediately (pre-cached in scope)
		expect(snapshots[0]?.loading).toBe(false);
		expect(snapshots[0]?.data).toBe(99);
		expect(screen.getByTestId("state").textContent).toBe("99");

		cleanup();
		await scope.dispose();
	});

	it("enabled: false returns idle state", async () => {
		const { scope, ref } = await setupScopeWithEntity(42);

		function Status({ entityRef }: { entityRef: EntityRef<CounterEntity> }) {
			const status = useFxtrLoadQuery(entityRef, { enabled: false });
			return (
				<span data-testid="state">
					{status.loading ? "loading" : status.data ? "data" : "idle"}
				</span>
			);
		}

		render(
			<FxtrProvider scope={scope}>
				<Status entityRef={ref} />
			</FxtrProvider>,
		);

		expect(screen.getByTestId("state").textContent).toBe("idle");
		cleanup();
		await scope.dispose();
	});

	it("enabled flip triggers load", async () => {
		const { scope, ref } = await setupScopeWithEntity(42);

		function Status({
			entityRef,
			enabled,
		}: { entityRef: EntityRef<CounterEntity>; enabled: boolean }) {
			const status = useFxtrLoadQuery(entityRef, { enabled });
			if (!enabled) return <span data-testid="state">disabled</span>;
			if (status.loading) return <span data-testid="state">loading</span>;
			return <span data-testid="state">{status.data?.count}</span>;
		}

		const { rerender: rerenderTree } = render(
			<FxtrProvider scope={scope}>
				<Status entityRef={ref} enabled={false} />
			</FxtrProvider>,
		);

		expect(screen.getByTestId("state").textContent).toBe("disabled");

		// Flip enabled — should start loading
		rerenderTree(
			<FxtrProvider scope={scope}>
				<Status entityRef={ref} enabled={true} />
			</FxtrProvider>,
		);

		await waitFor(() => {
			expect(screen.getByTestId("state").textContent).toBe("42");
		});
		cleanup();
		await scope.dispose();
	});
});

// ---------------------------------------------------------------------------
// useFxtrLoadQuery — callback form
// ---------------------------------------------------------------------------

describe("useFxtrLoadQuery callback form", () => {
	it("returns status through lifecycle", async () => {
		const scope = createFxtrScope({ name: "test-root", include: [GLOBAL_SCOPE] });
		const ref1 = await scope.store(makeCounter(10));
		const ref2 = await scope.store(makeCounter(20));
		const snapshots: Array<{ data: unknown; loading: boolean }> = [];

		function Status() {
			const status = useFxtrLoadQuery(
				async (load) => {
					const [a, b] = await Promise.all([load(ref1), load(ref2)]);
					return a.count + b.count;
				},
				[ref1, ref2],
			);
			snapshots.push({ data: status.data, loading: status.loading });
			if (status.loading) return <span data-testid="state">loading</span>;
			return <span data-testid="state">{String(status.data)}</span>;
		}

		render(
			<FxtrProvider scope={scope}>
				<Status />
			</FxtrProvider>,
		);

		expect(snapshots[0]?.loading).toBe(true);

		await waitFor(() => {
			expect(screen.getByTestId("state").textContent).toBe("30");
		});

		// biome-ignore lint/style/noNonNullAssertion: snapshots is non-empty after waitFor
		const last = snapshots.at(-1)!;
		expect(last.loading).toBe(false);
		expect(last.data).toBe(30);

		cleanup();
		await scope.dispose();
	});

	it("enabled: false with callback returns idle", async () => {
		const scope = createFxtrScope({ name: "test-root", include: [GLOBAL_SCOPE] });
		const ref = await scope.store(makeCounter(10));
		let callbackExecutions = 0;

		function Status() {
			const status = useFxtrLoadQuery(
				async (load) => {
					callbackExecutions++;
					return (await load(ref)).count;
				},
				[ref],
				{ enabled: false },
			);
			return (
				<span data-testid="state">
					{status.loading ? "loading" : status.data !== undefined ? String(status.data) : "idle"}
				</span>
			);
		}

		render(
			<FxtrProvider scope={scope}>
				<Status />
			</FxtrProvider>,
		);

		expect(screen.getByTestId("state").textContent).toBe("idle");
		expect(callbackExecutions).toBe(0);
		cleanup();
		await scope.dispose();
	});

	it("deps change triggers reload", async () => {
		const scope = createFxtrScope({ name: "test-root", include: [GLOBAL_SCOPE] });
		const ref1 = await scope.store(makeCounter(10));
		const ref2 = await scope.store(makeCounter(20));

		function Status({ entityRef }: { entityRef: EntityRef<CounterEntity> }) {
			const status = useFxtrLoadQuery(
				async (load) => {
					const entity = await load(entityRef);
					return entity.count * 2;
				},
				[entityRef],
			);
			if (status.loading) return <span data-testid="state">loading</span>;
			return <span data-testid="state">{String(status.data)}</span>;
		}

		const { rerender: rerenderTree } = render(
			<FxtrProvider scope={scope}>
				<Status entityRef={ref1} />
			</FxtrProvider>,
		);

		await waitFor(() => {
			expect(screen.getByTestId("state").textContent).toBe("20");
		});

		// Change deps — should trigger a new load
		rerenderTree(
			<FxtrProvider scope={scope}>
				<Status entityRef={ref2} />
			</FxtrProvider>,
		);

		await waitFor(() => {
			expect(screen.getByTestId("state").textContent).toBe("40");
		});
		cleanup();
		await scope.dispose();
	});

	it("stable deps return memoized value without re-executing callback", async () => {
		const scope = createFxtrScope({ name: "test-root", include: [GLOBAL_SCOPE] });
		const ref = await scope.store(makeCounter(42));

		let callbackExecutions = 0;
		function Status() {
			const status = useFxtrLoadQuery(
				async (load) => {
					callbackExecutions++;
					const entity = await load(ref);
					return entity.count;
				},
				[ref],
			);
			if (status.loading) return <span data-testid="state">loading</span>;
			return <span data-testid="state">{String(status.data)}</span>;
		}

		const { rerender: rerenderTree } = render(
			<FxtrProvider scope={scope}>
				<Status />
			</FxtrProvider>,
		);

		await waitFor(() => {
			expect(screen.getByTestId("state").textContent).toBe("42");
		});

		const execsAfterFirstLoad = callbackExecutions;

		// Re-render with same deps — callback should not re-execute
		rerenderTree(
			<FxtrProvider scope={scope}>
				<Status />
			</FxtrProvider>,
		);

		await waitFor(() => {
			expect(screen.getByTestId("state").textContent).toBe("42");
		});
		expect(callbackExecutions).toBe(execsAfterFirstLoad);
		cleanup();
		await scope.dispose();
	});

	it("callback form allows non-cacheable loads", async () => {
		const scope = createFxtrScope({ name: "test-root", include: [GLOBAL_SCOPE] });
		const ref = await scope.store(makeCounter(77));

		const nonCacheableLoadCodec: EntityLoadCodec<CounterEntity> = {
			async supportsEntityType(typeId: FxtrID, _scope: FxtrScope): Promise<boolean> {
				return typeId.key() === COUNTER_TYPE_ID.key();
			},
			async decode(cbor: FxtrCbor, _scope: FxtrScope): Promise<CounterEntity> {
				return makeCounter((cbor as Record<string, unknown>).count as number, false);
			},
		};
		const nonCacheableRef = new EntityRef(ref.entityId, nonCacheableLoadCodec);

		function Status() {
			const status = useFxtrLoadQuery(
				async (load) => {
					const entity = await load(nonCacheableRef);
					return entity.count;
				},
				[nonCacheableRef],
			);
			if (status.loading) return <span data-testid="state">loading</span>;
			return <span data-testid="state">{String(status.data)}</span>;
		}

		render(
			<FxtrProvider scope={scope}>
				<Status />
			</FxtrProvider>,
		);

		await waitFor(() => {
			expect(screen.getByTestId("state").textContent).toBe("77");
		});
		cleanup();
		await scope.dispose();
	});
});
