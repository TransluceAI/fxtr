/**
 * Top-level app component.
 *
 * Sets up the HTTP namespace + FxtrProvider, manages pin selection
 * and view mode toggle (rich vs raw).
 */

import {
	type Entity,
	type EntityRef,
	type FxtrReadNamespace,
	type UnknownEntity,
	unionLoadCodec,
	unknownLoadCodec,
} from "@fxtr/core";
import { createHttpReadNamespace, withBaseUrl } from "@fxtr/http-client";
import { FxtrProvider } from "@fxtr/react";
import { Suspense, useEffect, useState } from "react";
import { Article, Author, Tag } from "../entities.js";
import { RichEntityView } from "./ArticleView.js";
import { ErrorBoundary } from "./ErrorBoundary.js";
import { PinBrowser } from "./PinBrowser.js";
import { UnknownEntityView } from "./UnknownEntityView.js";

const SERVER_URL = "http://localhost:3000";

/** Union codec that dispatches to Article, Author, or Tag. */
const exampleUnionCodec = unionLoadCodec(Article, Author, Tag);

export function App() {
	const [ns, setNs] = useState<FxtrReadNamespace | null>(null);
	const [selectedPin, setSelectedPin] = useState<string | null>(null);
	const [richMode, setRichMode] = useState(true);

	useEffect(() => {
		const namespace = createHttpReadNamespace(withBaseUrl(SERVER_URL));
		setNs(namespace);
		return () => namespace.scope.scheduleClose();
	}, []);

	if (!ns) return <p className="loading">Connecting...</p>;

	return (
		<FxtrProvider scope={ns.scope}>
			<h1>Fxtr Entity Viewer</h1>
			<PinBrowser serverUrl={SERVER_URL} onSelect={setSelectedPin} selectedPin={selectedPin} />

			<div className="controls">
				<label>
					<input
						type="checkbox"
						checked={richMode}
						onChange={(e) => setRichMode(e.target.checked)}
					/>{" "}
					Rich view
				</label>
			</div>

			{selectedPin && (
				<PinEntityView
					key={`${selectedPin}:${richMode}`}
					ns={ns}
					pinName={selectedPin}
					richMode={richMode}
				/>
			)}
		</FxtrProvider>
	);
}

/**
 * Resolves a pin name to an entity ref and renders it.
 * Wrapped in FxtrSubscope so navigating away cleans up tracked data.
 */
function PinEntityView({
	ns,
	pinName,
	richMode,
}: {
	ns: FxtrReadNamespace;
	pinName: string;
	richMode: boolean;
}) {
	const [entityRef, setEntityRef] = useState<EntityRef<Entity> | EntityRef<UnknownEntity> | null>(
		null,
	);
	const [resolveError, setResolveError] = useState<string | null>(null);

	useEffect(() => {
		let cancelled = false;
		(async () => {
			try {
				const ref = richMode
					? await ns.retrieve(pinName, exampleUnionCodec)
					: await ns.retrieve(pinName, unknownLoadCodec);
				if (!cancelled) setEntityRef(ref as EntityRef<Entity>);
			} catch (e) {
				if (!cancelled) setResolveError(e instanceof Error ? e.message : String(e));
			}
		})();
		return () => {
			cancelled = true;
		};
	}, [ns, pinName, richMode]);

	if (resolveError) return <div className="entity-view">Error: {resolveError}</div>;
	if (!entityRef) return <div className="entity-view loading">Resolving pin...</div>;

	return (
		<div className="entity-view">
			<ErrorBoundary
				fallback={(error, reset) => (
					<div>
						<p>Failed to load entity: {error.message}</p>
						<button type="button" onClick={reset}>
							Retry
						</button>
					</div>
				)}
			>
				<Suspense fallback={<p className="loading">Loading entity...</p>}>
					{richMode ? (
						<RichEntityView entityRef={entityRef} />
					) : (
						<UnknownEntityView entityRef={entityRef as EntityRef<UnknownEntity>} />
					)}
				</Suspense>
			</ErrorBoundary>
		</div>
	);
}
