/**
 * Debug view for `createDynamicDispatcher`. Rendered by the
 * dispatcher's optional `debugView()` method (see
 * `dispatcher.ts`). Surfaces — all reactive — :
 *
 *   - Dispatcher status + session id + lifecycle.
 *   - The four session-backed caches that drive the state
 *     machine (`service_type_cache`,
 *     `service_type_active_lookups`, `type_renderer_cache`,
 *     `type_renderer_active_lookups`). The renderer caches are
 *     nested by `(typeShortId, keyUuid)`; the debug view renders
 *     them as a flat list of `"<typeShortId>::<keyUuid>"` strings
 *     so each cache entry stays one row.
 *   - The in-memory compiled-module cache + per-module status.
 *   - The reload subscription's inbox state.
 *   - The per-entry table: stub short id + key uuid + current
 *     state-machine phase + staleness flag + inner component
 *     identity (via `getStaleAwareInner`).
 *
 * Plain unstyled HTML — the workstation shell wraps this in a
 * panel with its own styles. The component is read-only;
 * nothing here triggers state-machine work.
 */

import type { CallbackInboxStub, EntityRefStub } from "@fxtr-ui/core";
import type { Atom, PrimitiveAtom } from "jotai";
import { useAtomValue } from "jotai";
import type { ReactElement } from "react";
import type { AnyDispatchEntry, DispatcherStatus } from "./dispatcher";
import type { CompiledModuleStatus, RendererResolution } from "./dynamic_dispatcher";
import { getStaleAwareInner } from "./staleness_wrapper";

export interface DynamicDispatcherDebugViewProps {
	readonly status: Atom<DispatcherStatus>;
	readonly sessionId: string;
	readonly sessionLifecycle: Atom<string>;
	readonly mountedIds: Atom<readonly string[]>;
	readonly getEntryById: (id: string) => AnyDispatchEntry | undefined;
	/**
	 * Resolver from entry id → that entry's private staleness atom.
	 * Contract: the dispatcher pairs a staleness atom with every
	 * cached entry inside `getEntry`, so for any id present in
	 * `mountedIds` the lookup is guaranteed to return a defined
	 * atom. The debug view treats a `undefined` result as a
	 * contract violation (logs + falls back) — silently dropping
	 * the stale signal would hide the bug.
	 */
	readonly getStaleAtomById: (id: string) => PrimitiveAtom<boolean> | undefined;
	readonly serviceTypeCache: Atom<Record<string, EntityRefStub>>;
	readonly serviceTypeActiveLookups: Atom<Record<string, unknown>>;
	readonly typeRendererCache: Atom<Record<string, Record<string, RendererResolution | null>>>;
	readonly typeRendererActiveLookups: Atom<Record<string, Record<string, unknown>>>;
	readonly compiledModules: Atom<Map<string, CompiledModuleStatus>>;
	readonly reloadInbox: Atom<CallbackInboxStub<EntityRefStub> | null>;
	/**
	 * Manual "reload everything" trigger. Wipes the dispatcher's type
	 * caches + re-resolves every entry (and resubscribes if the reload
	 * subscription is currently broken) — the same path a received
	 * reload notification or a `connectionState → broken` transition
	 * runs. The dispatcher supplies this from `debugView()`, closing
	 * over its internals; it is intentionally *not* on the `Dispatcher`
	 * interface.
	 */
	readonly onReloadEverything: () => void;
}

export function DynamicDispatcherDebugView(props: DynamicDispatcherDebugViewProps): ReactElement {
	const status = useAtomValue(props.status);
	const lifecycle = useAtomValue(props.sessionLifecycle);
	const ids = useAtomValue(props.mountedIds);

	return (
		<div data-testid="dynamic-dispatcher-debug">
			<header>
				<h3>Dynamic Dispatcher</h3>
				<button type="button" data-testid="reload-everything" onClick={props.onReloadEverything}>
					Reload everything
				</button>
				<dl>
					<dt>Status</dt>
					<dd data-testid="dispatcher-status">{status}</dd>
					<dt>Session</dt>
					<dd data-testid="session-id">{props.sessionId}</dd>
					<dt>Session lifecycle</dt>
					<dd data-testid="session-lifecycle">{lifecycle}</dd>
					<dt>Mounted entries</dt>
					<dd data-testid="mounted-count">{ids.length}</dd>
				</dl>
			</header>

			<ReloadInboxSection inbox={props.reloadInbox} />
			<TypeCachesSection
				serviceTypeCache={props.serviceTypeCache}
				serviceTypeActiveLookups={props.serviceTypeActiveLookups}
				typeRendererCache={props.typeRendererCache}
				typeRendererActiveLookups={props.typeRendererActiveLookups}
			/>
			<CompiledModulesSection compiledModules={props.compiledModules} />
			<EntriesSection
				ids={ids}
				getEntryById={props.getEntryById}
				getStaleAtomById={props.getStaleAtomById}
			/>
		</div>
	);
}

// ---------- Reload inbox -----------------------------------------------

function ReloadInboxSection({
	inbox,
}: {
	readonly inbox: Atom<CallbackInboxStub<EntityRefStub> | null>;
}): ReactElement {
	const value = useAtomValue(inbox);
	if (value === null) {
		return (
			<section data-testid="reload-inbox-section">
				<h4>Reload inbox</h4>
				<p data-testid="reload-inbox-empty">No inbox yet — dispatcher has not connected.</p>
			</section>
		);
	}
	return (
		<section data-testid="reload-inbox-section">
			<h4>Reload inbox</h4>
			<ReloadInboxBody inbox={value} />
		</section>
	);
}

function ReloadInboxBody({
	inbox,
}: {
	readonly inbox: CallbackInboxStub<EntityRefStub>;
}): ReactElement {
	const inboxState = useAtomValue(inbox.state);
	const events = useAtomValue(inbox.events);
	return (
		<dl>
			<dt>State</dt>
			<dd data-testid="reload-inbox-state">{inboxState}</dd>
			<dt>Cursor offset</dt>
			<dd data-testid="reload-inbox-offset">{inbox.offset}</dd>
			<dt>Pending events</dt>
			<dd data-testid="reload-inbox-pending">{events.length}</dd>
		</dl>
	);
}

// ---------- Type caches ------------------------------------------------

function TypeCachesSection({
	serviceTypeCache,
	serviceTypeActiveLookups,
	typeRendererCache,
	typeRendererActiveLookups,
}: {
	readonly serviceTypeCache: Atom<Record<string, EntityRefStub>>;
	readonly serviceTypeActiveLookups: Atom<Record<string, unknown>>;
	readonly typeRendererCache: Atom<Record<string, Record<string, RendererResolution | null>>>;
	readonly typeRendererActiveLookups: Atom<Record<string, Record<string, unknown>>>;
}): ReactElement {
	const svcCache = useAtomValue(serviceTypeCache);
	const svcActive = useAtomValue(serviceTypeActiveLookups);
	const rendCache = useAtomValue(typeRendererCache);
	const rendActive = useAtomValue(typeRendererActiveLookups);
	const rendCacheKeys = flattenNestedKeys(rendCache);
	const rendActiveKeys = flattenNestedKeys(rendActive);
	return (
		<section data-testid="type-caches-section">
			<h4>Type caches</h4>
			<CacheKeyList
				label="service_type_cache"
				testid="svc-type-cache"
				keys={Object.keys(svcCache)}
			/>
			<CacheKeyList
				label="service_type_active_lookups"
				testid="svc-type-active"
				keys={Object.keys(svcActive)}
			/>
			<CacheKeyList
				label="type_renderer_cache"
				testid="type-renderer-cache"
				keys={rendCacheKeys}
				annotate={(joined) => {
					const sep = joined.indexOf("::");
					if (sep < 0) return "";
					const type = joined.slice(0, sep);
					const keyUuid = joined.slice(sep + 2);
					return rendCache[type]?.[keyUuid] === null ? " (null = no match)" : "";
				}}
			/>
			<CacheKeyList
				label="type_renderer_active_lookups"
				testid="type-renderer-active"
				keys={rendActiveKeys}
			/>
		</section>
	);
}

/**
 * Flatten a nested `Record<typeShortId, Record<keyUuid, _>>` into a
 * list of ``"<typeShortId>::<keyUuid>"`` strings. The renderer caches
 * are nested so the dispatcher can route different dispatch keys for
 * the same type to different modules; the debug view renders the
 * combined form so each cache entry is visible at a glance without
 * an extra UI level.
 */
function flattenNestedKeys<V>(nested: Record<string, Record<string, V>>): string[] {
	const out: string[] = [];
	for (const [outer, inner] of Object.entries(nested)) {
		for (const innerKey of Object.keys(inner)) {
			out.push(`${outer}::${innerKey}`);
		}
	}
	return out;
}

function CacheKeyList({
	label,
	testid,
	keys,
	annotate,
}: {
	readonly label: string;
	readonly testid: string;
	readonly keys: string[];
	readonly annotate?: (key: string) => string;
}): ReactElement {
	return (
		<div data-testid={testid}>
			<strong>{label}</strong> <span data-testid={`${testid}-count`}>({keys.length})</span>
			{keys.length > 0 && (
				<ul>
					{keys.map((k) => (
						<li key={k}>
							<code>{k}</code>
							{annotate?.(k)}
						</li>
					))}
				</ul>
			)}
		</div>
	);
}

// ---------- Compiled modules -------------------------------------------

function CompiledModulesSection({
	compiledModules,
}: {
	readonly compiledModules: Atom<Map<string, CompiledModuleStatus>>;
}): ReactElement {
	const modules = useAtomValue(compiledModules);
	const entries = Array.from(modules.entries());
	return (
		<section data-testid="compiled-modules-section">
			<h4>Compiled modules</h4>
			<p>
				<span data-testid="compiled-modules-count">{entries.length}</span> renderer module(s) loaded
			</p>
			{entries.length > 0 && (
				<table>
					<thead>
						<tr>
							<th>Renderer short id</th>
							<th>Status</th>
							<th>Subtype keys</th>
						</tr>
					</thead>
					<tbody>
						{entries.map(([shortId, status]) => (
							<tr key={shortId} data-testid={`compiled-module-${shortId}`}>
								<td>
									<code>{shortId}</code>
								</td>
								<td>{status.status}</td>
								<td>
									{status.status === "ready"
										? Object.keys(status.module).join(", ") || "(none)"
										: "—"}
								</td>
							</tr>
						))}
					</tbody>
				</table>
			)}
		</section>
	);
}

// ---------- Entries ----------------------------------------------------

function EntriesSection({
	ids,
	getEntryById,
	getStaleAtomById,
}: {
	readonly ids: readonly string[];
	readonly getEntryById: (id: string) => AnyDispatchEntry | undefined;
	readonly getStaleAtomById: (id: string) => PrimitiveAtom<boolean> | undefined;
}): ReactElement {
	return (
		<section data-testid="entries-section">
			<h4>Mounted entries</h4>
			{ids.length === 0 ? (
				<p data-testid="no-entries">No entries resolved yet.</p>
			) : (
				<table>
					<thead>
						<tr>
							<th>Entry id</th>
							<th>Stub</th>
							<th>Phase</th>
							<th>Stale?</th>
							<th>Component</th>
						</tr>
					</thead>
					<tbody>
						{ids.map((id) => (
							<DynamicEntryRow
								key={id}
								id={id}
								entry={getEntryById(id)}
								staleAtom={getStaleAtomById(id)}
							/>
						))}
					</tbody>
				</table>
			)}
		</section>
	);
}

function DynamicEntryRow({
	id,
	entry,
	staleAtom,
}: {
	readonly id: string;
	readonly entry: AnyDispatchEntry | undefined;
	readonly staleAtom: PrimitiveAtom<boolean> | undefined;
}): ReactElement {
	if (entry === undefined) {
		return (
			<tr data-testid={`entry-row-${id}`}>
				<td colSpan={5}>(entry pruned)</td>
			</tr>
		);
	}
	if (staleAtom === undefined) {
		// Contract violation: the dispatcher's `getEntry` is
		// supposed to mint a staleAtom alongside every entry. If
		// this row's id is in `mountedIds` but the lookup returned
		// `undefined`, we'd rather show an error row than silently
		// fall back to a constant `false` (which would hide the
		// bug).
		return (
			<tr data-testid={`entry-row-${id}`}>
				<td colSpan={5}>{`(bug: no staleAtom for entry ${id})`}</td>
			</tr>
		);
	}
	const dispatcherState = useAtomValue(entry.dispatchInternalStateAtom) as
		| { phase?: string }
		| undefined;
	const wrappedComponent = useAtomValue(entry.componentAtom);
	const inner = getStaleAwareInner(wrappedComponent);
	const stale = useAtomValue(staleAtom);
	const innerLabel =
		(inner as { displayName?: string; name?: string } | undefined)?.displayName ??
		(inner as { name?: string } | undefined)?.name ??
		"(unwrapped)";
	return (
		<tr data-testid={`entry-row-${id}`}>
			<td>
				<code>{id}</code>
			</td>
			<td>
				<code>{entry.stub.shortId}</code>
			</td>
			<td data-testid={`entry-phase-${id}`}>{dispatcherState?.phase ?? "(no phase)"}</td>
			<td data-testid={`entry-stale-${id}`}>{stale ? "yes" : "no"}</td>
			<td>{innerLabel}</td>
		</tr>
	);
}
