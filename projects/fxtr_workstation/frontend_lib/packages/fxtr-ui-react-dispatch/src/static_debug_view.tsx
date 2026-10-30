/**
 * Debug view for `createStaticDispatcher`. Rendered by the
 * dispatcher's optional `debugView()` method (see
 * `dispatcher.ts`). Surfaces:
 *
 *   - Dispatcher status (live, from `dispatcher.status`).
 *   - Session id + lifecycle (live, from `dispatcher.session`).
 *   - Cached entry table — one row per `(keyUuid, shortId)` that
 *     has been resolved through `getEntry`. Each row shows the
 *     key uuid, stub short id, currently-published component's
 *     `displayName` / `name`, and the current progress message
 *     (if any).
 *
 * Plain unstyled HTML — the workstation shell wraps this in a
 * panel with its own styles. The component is read-only; nothing
 * here triggers state machine work.
 */

import type { Atom } from "jotai";
import { useAtomValue } from "jotai";
import type { ReactElement } from "react";
import type { AnyDispatchEntry, DispatcherStatus } from "./dispatcher";

export interface StaticDispatcherDebugViewProps {
	readonly status: Atom<DispatcherStatus>;
	readonly sessionId: string;
	readonly sessionLifecycle: Atom<string>;
	readonly mountedIds: Atom<readonly string[]>;
	/**
	 * Resolver from id → entry. Looks up the cache snapshot at
	 * render time. Returning `undefined` means the id was just
	 * pruned (shouldn't happen at the granularity we update
	 * `mountedIds`, but defensive).
	 */
	readonly getEntryById: (id: string) => AnyDispatchEntry | undefined;
}

export function StaticDispatcherDebugView(props: StaticDispatcherDebugViewProps): ReactElement {
	const status = useAtomValue(props.status);
	const lifecycle = useAtomValue(props.sessionLifecycle);
	const ids = useAtomValue(props.mountedIds);

	return (
		<div data-testid="static-dispatcher-debug">
			<header>
				<h3>Static Dispatcher</h3>
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
			<section>
				<h4>Cached entries</h4>
				{ids.length === 0 ? (
					<p data-testid="no-entries">No entries resolved yet.</p>
				) : (
					<table>
						<thead>
							<tr>
								<th>Entry id</th>
								<th>Stub</th>
								<th>Component</th>
							</tr>
						</thead>
						<tbody>
							{ids.map((id) => (
								<StaticDispatcherEntryRow key={id} id={id} entry={props.getEntryById(id)} />
							))}
						</tbody>
					</table>
				)}
			</section>
		</div>
	);
}

function StaticDispatcherEntryRow({
	id,
	entry,
}: {
	readonly id: string;
	readonly entry: AnyDispatchEntry | undefined;
}): ReactElement {
	if (entry === undefined) {
		return (
			<tr data-testid={`entry-row-${id}`}>
				<td colSpan={3}>(entry pruned)</td>
			</tr>
		);
	}
	const Component = useAtomValue(entry.componentAtom);
	const componentLabel =
		(Component as { displayName?: string; name?: string }).displayName ??
		(Component as { name?: string }).name ??
		"(anonymous)";
	return (
		<tr data-testid={`entry-row-${id}`}>
			<td>
				<code>{id}</code>
			</td>
			<td>
				<code>{entry.stub.shortId}</code>
			</td>
			<td>{componentLabel}</td>
		</tr>
	);
}
