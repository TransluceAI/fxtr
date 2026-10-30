/**
 * End-to-end integration test for `createDynamicDispatcher`
 * against a live `@fxtr-ui/dynamic-dispatch-demo` Python backend.
 *
 * Test scope:
 *   - Spawn the demo workstation (it mounts
 *     `BasicDynamicDispatchImpl` as the `renderer-dispatcher`
 *     external service and seeds `demo-note-1` / `demo-note-2` /
 *     `demo-photo-1` / `demo-photo-2` entities).
 *   - Connect via `WorkstationConnector`.
 *   - Look up the dispatcher service ref by external name.
 *   - Build a `DynamicDispatcher` with the connector's
 *     `loadEntityData` (real wire load) and an injected
 *     `compileRendererBytes` mock (jsdom / node test env has no
 *     real browser ESM loader; the mock returns hand-built module
 *     maps keyed off bytes that the test recognises).
 *   - For each seeded entity:
 *     - `dispatcher.getEntry(CardKey, stub)` and subscribe.
 *     - Poll until the dispatched component matches one of our
 *       known sentinels.
 *
 * What this verifies:
 *   - Workstation external-service mount path.
 *   - Frontend service-by-name lookup.
 *   - Entity-side state machine: load → extract $fxtr.type →
 *     resolve invoke → load renderer bytes.
 *   - The bytes that arrive on the frontend are the esbuild output
 *     of the demo's renderer templates (basic content-check).
 *   - The dispatcher publishes a component for each entity.
 *
 * What this does NOT verify (intentional out-of-scope):
 *   - Real ESM compilation of the bytes. Doing that in node's
 *     test env requires a working dynamic-import loader for
 *     blob URLs (browser-only) or some node-side workaround.
 *     The injected `compileRendererBytes` mock side-steps this.
 *     The compile path is covered by the demo's `pnpm dev`
 *     browser smoke test.
 */

import { WorkstationConnector } from "@fxtr-ui/connector-workstation";
import {
	type Dispatcher,
	createDynamicDispatcher,
	declareDispatchKey,
	getStaleAwareInner,
} from "@fxtr-ui/react-dispatch";
import type { Atom } from "jotai";
import type { ComponentType } from "react";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import {
	type DynamicDispatchDemoWorkstation,
	startDynamicDispatchDemoWorkstation,
} from "./_dynamic_integration_harness";

// Test components — sentinel identities, never actually rendered.
function NoteSentinel() {
	return null;
}
function PhotoSentinel() {
	return null;
}
function FallbackSentinel() {
	return null;
}
function NoMatchSentinel() {
	return null;
}
function ErrorSentinel() {
	return null;
}

// MUST match the uuid in the renderer template TSX files (note /
// photo). Both sides agree on this out-of-band; if the demo's
// dispatch_keys.ts changes its uuid, change here + the templates.
const CardKey = declareDispatchKey({
	uuid: "00000000-0000-4000-8000-000000010001",
	description: "Card",
	fallbacks: {
		Loading: FallbackSentinel,
		NoMatch: NoMatchSentinel,
		Error: ErrorSentinel,
	},
});

describe("DynamicDispatcher integration (live workstation)", () => {
	let backend: DynamicDispatchDemoWorkstation;
	let connector: WorkstationConnector;

	beforeAll(async () => {
		backend = await startDynamicDispatchDemoWorkstation();
		connector = new WorkstationConnector({ url: backend.clientUrl });
	}, 90_000);

	afterAll(async () => {
		connector?.close();
		await backend?.close();
	});

	it("dispatches an entity through the full wire path to the expected sentinel", async () => {
		// Wait for the connector's service / entity lists to
		// populate. The dispatcher service is mounted as an
		// external service at workstation startup, so it should
		// appear on the first snapshot.
		const services = connector.listServices();
		const entities = connector.listEntities();
		await pollUntil(
			() =>
				connector.store
					.get(services.records)
					.some((r) => r.external_names.includes(backend.rendererDispatcherName)),
			"renderer-dispatcher service to appear in connector.listServices()",
		);
		await pollUntil(
			() =>
				connector.store.get(entities.records).some((r) => r.external_names.includes("demo-note-1")),
			"demo-note-1 entity to appear in connector.listEntities()",
		);

		// Look up the dispatcher service ref.
		const serviceRecord = connector.store
			.get(services.records)
			.find((r) => r.external_names.includes(backend.rendererDispatcherName));
		if (serviceRecord === undefined) {
			throw new Error("unreachable: pollUntil asserted presence");
		}
		const dispatchServiceRef = connector.serviceRef(serviceRecord.short_id);

		// Track compiled-bytes shapes so we can sanity-check the
		// dispatcher fetched the right thing.
		const compileCalls: Array<{ rendererShortId: string; firstBytes: string }> = [];

		// Mock compileRendererBytes: returns a sentinel-component
		// map based on which type's bytes we received. Recognises
		// "NoteCard" vs "PhotoCard" substring in the compiled JS.
		const compileRendererBytes = async (bytes: Uint8Array) => {
			const text = new TextDecoder().decode(bytes);
			const which: ComponentType<{ stub: unknown }> = text.includes("NoteCard")
				? NoteSentinel
				: text.includes("PhotoCard")
					? PhotoSentinel
					: NoMatchSentinel;
			compileCalls.push({
				rendererShortId: "(unknown — only bytes available at compile time)",
				firstBytes: text.slice(0, 80),
			});
			return {
				default: {
					[CardKey.uuid]: which,
				},
			};
		};

		// Build the dispatcher.
		const session = await connector.createSession({});
		// `createSession` returns with the session in `connecting`;
		// any `allocate*` call before the first snapshot arrives
		// throws. Wait for the lifecycle to flip before driving the
		// dispatcher's state machine.
		await pollUntil(
			() => session.store.get(session.lifecycle) === "connected",
			'session lifecycle to reach "connected"',
		);
		let dispatcher: Dispatcher | null = null;
		try {
			dispatcher = createDynamicDispatcher({
				session,
				dispatchServiceRef,
				loadEntityData: (ref) => connector.loadEntityData(ref),
				compileRendererBytes,
			});

			// Pick a Note entity and a Photo entity.
			const allEntities = connector.store.get(entities.records);
			const noteRecord = allEntities.find((r) => r.external_names.includes("demo-note-1"));
			const photoRecord = allEntities.find((r) => r.external_names.includes("demo-photo-1"));
			if (noteRecord === undefined || photoRecord === undefined) {
				throw new Error(
					"workstation didn't expose demo-note-1 or demo-photo-1 entities (check the Python backend seed list)",
				);
			}

			// Get entries + subscribe.
			const noteEntry = dispatcher.getEntry(CardKey, connector.entityRef(noteRecord.short_id));
			const photoEntry = dispatcher.getEntry(CardKey, connector.entityRef(photoRecord.short_id));
			const unsubNote = subscribeAtom(connector.store, noteEntry.componentAtom);
			const unsubPhoto = subscribeAtom(connector.store, photoEntry.componentAtom);
			try {
				// Poll until both entries publish their respective
				// sentinel components. The dynamic dispatcher wraps
				// every published component in a `StaleAware(*)` HOC
				// (see `staleness_wrapper.tsx`); we check identity of
				// the inner component the wrap stamped via
				// `getStaleAwareInner`.
				await pollUntil(
					() => getStaleAwareInner(connector.store.get(noteEntry.componentAtom)) === NoteSentinel,
					"NoteSentinel to be published for demo-note-1",
				);
				await pollUntil(
					() => getStaleAwareInner(connector.store.get(photoEntry.componentAtom)) === PhotoSentinel,
					"PhotoSentinel to be published for demo-photo-1",
				);

				expect(compileCalls.length).toBeGreaterThanOrEqual(2);
				// Each compile's first bytes should look like esbuild
				// output: either a leading `// <path>` source-map
				// comment or a module-level decl keyword. (We can't
				// hard-pin "// " because esbuild may omit the
				// comment header in some flag combinations.)
				for (const c of compileCalls) {
					expect(c.firstBytes).toMatch(/^\/\/ |import|export|var |const |function /);
				}
			} finally {
				unsubNote();
				unsubPhoto();
			}
		} finally {
			if (dispatcher !== null) {
				await dispatcher.detach();
			} else {
				await session.detach();
			}
		}
	}, 60_000);
});

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function pollUntil(
	predicate: () => boolean,
	description: string,
	timeoutMs = 20_000,
	intervalMs = 50,
): Promise<void> {
	const start = Date.now();
	while (Date.now() - start < timeoutMs) {
		if (predicate()) return;
		await new Promise<void>((resolve) => setTimeout(resolve, intervalMs));
	}
	throw new Error(`pollUntil timed out after ${timeoutMs}ms waiting for: ${description}`);
}

function subscribeAtom(store: WorkstationConnector["store"], atomToSub: Atom<unknown>): () => void {
	return store.sub(atomToSub, () => {
		/* no-op; tests read with store.get */
	});
}
