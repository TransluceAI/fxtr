/**
 * Layout + dispatcher-session-id persistence round-trips through an
 * injected `Storage` (here `memoryStorage()` from `@fxtr-ui/core`).
 *
 * Guards the design/04 contract: the panel viewer hardcodes no
 * `localStorage` — every read/write goes through the handle the caller
 * supplies, so the HTTP demo can age its layout + sessions in one shared
 * backend.
 */

import { memoryStorage } from "@fxtr-ui/core";
import {
	DISPATCHER_SESSION_ID_KEY,
	LAYOUT_STORAGE_KEY,
	LAYOUT_STORAGE_VERSION,
	type PersistedLayoutV6,
	clearDispatcherSessionId,
	clearPersistedLayout,
	loadPersistedLayout,
	readDispatcherSessionId,
	savePersistedLayout,
	writeDispatcherSessionId,
} from "@fxtr-ui/panel-viewer";
import { describe, expect, it } from "vitest";

function samplePayload(): PersistedLayoutV6 {
	return {
		version: LAYOUT_STORAGE_VERSION,
		// `dockview` is opaque to the persistence layer (it round-trips
		// dockview's own `toJSON` blob); a plain object is enough here.
		dockview: { grid: { root: {} } } as unknown as PersistedLayoutV6["dockview"],
		sidebar: { collapsed: true, width: 321 },
	};
}

describe("layout persistence", () => {
	it("round-trips a payload through the injected storage", () => {
		const storage = memoryStorage();
		const payload = samplePayload();
		savePersistedLayout(storage, payload);
		expect(loadPersistedLayout(storage)).toEqual(payload);
	});

	it("writes under LAYOUT_STORAGE_KEY (nothing leaks to other storages)", () => {
		const a = memoryStorage();
		const b = memoryStorage();
		savePersistedLayout(a, samplePayload());
		expect(a.getItem(LAYOUT_STORAGE_KEY)).not.toBeNull();
		// A different handle is untouched — no hidden global localStorage.
		expect(loadPersistedLayout(b)).toBeNull();
	});

	it("returns null for a missing key", () => {
		expect(loadPersistedLayout(memoryStorage())).toBeNull();
	});

	it("returns null on a version mismatch (drops stale layouts)", () => {
		const storage = memoryStorage();
		const stale = { ...samplePayload(), version: LAYOUT_STORAGE_VERSION - 1 };
		storage.setItem(LAYOUT_STORAGE_KEY, JSON.stringify(stale));
		expect(loadPersistedLayout(storage)).toBeNull();
	});

	it("returns null on malformed JSON", () => {
		const storage = memoryStorage();
		storage.setItem(LAYOUT_STORAGE_KEY, "{ not json");
		expect(loadPersistedLayout(storage)).toBeNull();
	});

	it("returns null when required fields are missing", () => {
		const storage = memoryStorage();
		storage.setItem(
			LAYOUT_STORAGE_KEY,
			JSON.stringify({ version: LAYOUT_STORAGE_VERSION, dockview: {} }),
		);
		expect(loadPersistedLayout(storage)).toBeNull();
	});

	it("clear removes the layout", () => {
		const storage = memoryStorage();
		savePersistedLayout(storage, samplePayload());
		clearPersistedLayout(storage);
		expect(loadPersistedLayout(storage)).toBeNull();
	});
});

describe("dispatcher session id persistence", () => {
	it("round-trips an id through the injected storage", () => {
		const storage = memoryStorage();
		writeDispatcherSessionId(storage, "session-abc");
		expect(readDispatcherSessionId(storage)).toBe("session-abc");
		expect(storage.getItem(DISPATCHER_SESSION_ID_KEY)).toBe("session-abc");
	});

	it("reads null for a missing or empty id", () => {
		const storage = memoryStorage();
		expect(readDispatcherSessionId(storage)).toBeNull();
		storage.setItem(DISPATCHER_SESSION_ID_KEY, "");
		expect(readDispatcherSessionId(storage)).toBeNull();
	});

	it("clear removes the id", () => {
		const storage = memoryStorage();
		writeDispatcherSessionId(storage, "session-abc");
		clearDispatcherSessionId(storage);
		expect(readDispatcherSessionId(storage)).toBeNull();
	});

	it("ages independently per handle", () => {
		const a = memoryStorage();
		const b = memoryStorage();
		writeDispatcherSessionId(a, "only-in-a");
		expect(readDispatcherSessionId(b)).toBeNull();
	});
});
