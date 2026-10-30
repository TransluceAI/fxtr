import type { EntityRef, FxtrReadNamespace } from "@fxtr/core";
import { createHttpReadNamespace, withBaseUrl } from "@fxtr/http-client";
import { FxtrProvider, useFxtrLoad } from "@fxtr/react";
import { Suspense, useEffect, useState } from "react";
import { type ComponentTree, ComponentTree as ComponentTreeCodec } from "../entities.js";
import { FxtrTreeRenderer } from "./renderer.js";

const PAGES = [
	{ label: "Build: Report", pin: "pages/report" },
	{ label: "Dynamic: Example", pin: "pages/dynamic-example" },
];

export function App() {
	const [ns, setNs] = useState<FxtrReadNamespace | null>(null);
	const [selectedPin, setSelectedPin] = useState(PAGES[0]?.pin ?? "pages/report");

	useEffect(() => {
		const namespace = createHttpReadNamespace(withBaseUrl(`${window.location.origin}`));
		setNs(namespace);
		return () => namespace.scope.scheduleClose();
	}, []);

	if (!ns) return <div className="loading">Connecting to fxtr server...</div>;

	return (
		<FxtrProvider scope={ns.scope}>
			<div className="app">
				<header className="app-header">
					<h1>MDX-to-Fxtr Spike</h1>
					<div className="page-selector">
						<label htmlFor="page-select">Page: </label>
						<select
							id="page-select"
							value={selectedPin}
							onChange={(e) => setSelectedPin(e.target.value)}
						>
							{PAGES.map((p) => (
								<option key={p.pin} value={p.pin}>
									{p.label}
								</option>
							))}
						</select>
						<span className="endpoint-label">/pin/{selectedPin}</span>
					</div>
				</header>

				<Suspense fallback={<div className="loading">Loading page...</div>}>
					<PageView key={selectedPin} ns={ns} pin={selectedPin} />
				</Suspense>
			</div>
		</FxtrProvider>
	);
}

function PageView({ ns, pin }: { ns: FxtrReadNamespace; pin: string }) {
	const [entityRef, setEntityRef] = useState<EntityRef<ComponentTree> | null>(null);
	const [error, setError] = useState<string | null>(null);

	useEffect(() => {
		let cancelled = false;
		(async () => {
			try {
				const ref = await ns.retrieve(pin, ComponentTreeCodec);
				if (!cancelled) setEntityRef(ref as EntityRef<ComponentTree>);
			} catch (e) {
				if (!cancelled) setError(e instanceof Error ? e.message : String(e));
			}
		})();
		return () => {
			cancelled = true;
		};
	}, [ns, pin]);

	if (error) return <div className="error">Error: {error}</div>;
	if (!entityRef) return <div className="loading">Resolving pin...</div>;

	return (
		<Suspense fallback={<div className="loading">Loading component tree...</div>}>
			<ComponentTreeView entityRef={entityRef} />
		</Suspense>
	);
}

function ComponentTreeView({ entityRef }: { entityRef: EntityRef<ComponentTree> }) {
	const tree = useFxtrLoad(entityRef);
	return (
		<div className="app-content">
			<div className="rendered-content">
				<FxtrTreeRenderer document={tree.document} />
			</div>
		</div>
	);
}
