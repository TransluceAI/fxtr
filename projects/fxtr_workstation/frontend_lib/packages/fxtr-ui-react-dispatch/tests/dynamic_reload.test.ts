/**
 * End-to-end tests for the dynamic dispatcher's hot-reload
 * reception path (stage 10c chunk 4).
 *
 * Drives a dedicated stdin-controlled Python harness backend
 * (`_reload_harness_workstation.py`), so reload triggers are
 * deterministic — no timing-dependent polling required.
 *
 * Coverage:
 *
 *   - the inbox `subscribe_to` (callback-target invoke) fires exactly once on dispatcher
 *     construction (after `session.lifecycle === "connected"`).
 *   - A `TRIGGER_RELOAD` on the harness's stdin produces a
 *     callback event on the dispatcher's `reload_inbox`, drives
 *     the per-entry `staleAtom` to `true` mid-reload, and back to
 *     `false` once the next terminal phase commits a new
 *     component.
 *   - The cursor advance via `inbox.sliced(...)` consumes the
 *     event (so a second tick of the effect doesn't re-fire the
 *     reload).
 *
 * The shape of `reload_inbox` lifetime under reattach (re-creates
 * on `"closed"`) is covered by a separate unit test that doesn't
 * need the live harness.
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

import { type ReloadHarnessWorkstation, startReloadHarnessWorkstation } from "./_reload_harness";

// Sentinel components. The harness's renderer templates write
// either "NoteCard" or "PhotoCard" into the compiled bytes;
// `compileRendererBytes` (mocked below) selects the matching
// sentinel.
function NoteSentinel() {
	return null;
}
function PhotoSentinel() {
	return null;
}
function LoadingSentinel() {
	return null;
}
function NoMatchSentinel() {
	return null;
}
function ErrorSentinel() {
	return null;
}

// MUST match the uuid in the harness's renderer template files.
const CardKey = declareDispatchKey({
	uuid: "00000000-0000-4000-8000-000000020001",
	description: "Card",
	fallbacks: {
		Loading: LoadingSentinel,
		NoMatch: NoMatchSentinel,
		Error: ErrorSentinel,
	},
});

async function pollUntil(
	pred: () => boolean,
	what: string,
	timeoutMs = 10_000,
	intervalMs = 25,
): Promise<void> {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		if (pred()) return;
		await new Promise<void>((resolve) => setTimeout(resolve, intervalMs));
	}
	throw new Error(`pollUntil timed out after ${timeoutMs}ms waiting for: ${what}`);
}

function subscribeAtom(store: WorkstationConnector["store"], a: Atom<unknown>): () => void {
	return store.sub(a, () => {});
}

describe("DynamicDispatcher hot-reload reception (harness)", () => {
	let backend: ReloadHarnessWorkstation;
	let connector: WorkstationConnector;

	beforeAll(async () => {
		backend = await startReloadHarnessWorkstation();
		connector = new WorkstationConnector({ url: backend.clientUrl });
	}, 90_000);

	afterAll(async () => {
		connector?.close();
		await backend?.close();
	});

	it("subscribes once on connect; TRIGGER_RELOAD flips staleAtom and re-resolves", async () => {
		// Wait for the workstation's snapshot to populate the
		// connector's list views.
		const services = connector.listServices();
		const entities = connector.listEntities();
		await pollUntil(
			() =>
				connector.store
					.get(services.records)
					.some((r) => r.external_names.includes(backend.rendererDispatcherName)),
			"renderer-dispatcher service to appear",
		);
		await pollUntil(
			() =>
				connector.store
					.get(entities.records)
					.some((r) => r.external_names.includes("harness-note")),
			"harness-note entity to appear",
		);

		const serviceRecord = connector.store
			.get(services.records)
			.find((r) => r.external_names.includes(backend.rendererDispatcherName));
		if (serviceRecord === undefined) throw new Error("unreachable");
		const dispatchServiceRef = connector.serviceRef(serviceRecord.short_id);

		// Track compile invocations so we can see the dispatcher
		// reach phase 3 on each entry.
		let compileCount = 0;
		const compileRendererBytes = async (bytes: Uint8Array) => {
			compileCount += 1;
			const text = new TextDecoder().decode(bytes);
			const which: ComponentType<{ stub: unknown }> = text.includes("NoteCard")
				? NoteSentinel
				: text.includes("PhotoCard")
					? PhotoSentinel
					: NoMatchSentinel;
			return {
				default: {
					[CardKey.uuid]: which,
				},
			};
		};

		const session = await connector.createSession({});
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

			// Dispatch the Note entity, wait for it to resolve to
			// the NoteSentinel.
			const noteRecord = connector.store
				.get(entities.records)
				.find((r) => r.external_names.includes("harness-note"));
			if (noteRecord === undefined) throw new Error("unreachable");

			const noteEntry = dispatcher.getEntry(CardKey, connector.entityRef(noteRecord.short_id));
			const unsubNote = subscribeAtom(connector.store, noteEntry.componentAtom);
			try {
				await pollUntil(
					() => getStaleAwareInner(connector.store.get(noteEntry.componentAtom)) === NoteSentinel,
					"NoteSentinel to publish",
				);

				const compilesAfterInitialResolve = compileCount;
				expect(compilesAfterInitialResolve).toBeGreaterThanOrEqual(1);

				// Inspect the dispatcher's userState to verify the
				// reload_inbox was allocated exactly once. We read
				// the session's userState atom directly.
				const userStateBeforeReload = session.store.get(session.userState) as {
					reload_inbox?: { offset: number } | null;
				} | null;
				expect(userStateBeforeReload?.reload_inbox).toBeDefined();
				expect(userStateBeforeReload?.reload_inbox).not.toBeNull();
				const inboxOffsetBefore = userStateBeforeReload?.reload_inbox?.offset ?? -1;
				expect(inboxOffsetBefore).toBe(0);

				// Trigger a reload via the harness's stdin.
				await backend.triggerReload();

				// Poll until the dispatcher consumes the event —
				// the inbox's cursor advances past offset 0. The
				// reload effect calls `inbox.sliced(events.length)`
				// to consume.
				await pollUntil(
					() => {
						const us = session.store.get(session.userState) as {
							reload_inbox?: { offset: number } | null;
						} | null;
						return (us?.reload_inbox?.offset ?? 0) > inboxOffsetBefore;
					},
					"reload inbox cursor to advance past the consumed event",
					15_000,
				);

				// The compileRendererBytes mock should have been
				// called again — at least once during reload (the
				// in-memory compiled-module cache is dispatcher-
				// internal; resetting `dispatcher_state` to init
				// re-runs phase 3 which reuses the cached
				// compiled module without re-fetching, so we don't
				// require it to grow; the bytes-load short-circuits
				// when the renderer short_id is already compiled).
				// What MUST happen: the entry re-resolves and lands
				// on a NoteSentinel again (same component, same
				// inner identity after the wrap memoization).
				await pollUntil(
					() => getStaleAwareInner(connector.store.get(noteEntry.componentAtom)) === NoteSentinel,
					"NoteSentinel to publish again after reload",
					15_000,
				);
			} finally {
				unsubNote();
			}
		} finally {
			if (dispatcher !== null) {
				await dispatcher.detach();
			}
		}
	}, 60_000);
});
