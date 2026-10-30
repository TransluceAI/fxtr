/**
 * Dev demo client app.
 *
 * Connects to the fxtr HTTP server on the same origin and mounts the
 * workstation shell with the live namespace. The namespace panel is now
 * the entry point — the user drags / clicks / right-clicks from there.
 */

import { WorkstationShell, createWorkstationDispatchHandler } from "@fxtr-workstation/core";
import { type HttpFxtrReadNamespace, createHttpReadNamespace } from "@fxtr/http-client";
import { FxtrProvider } from "@fxtr/react";
import { DispatchProvider } from "@fxtr/react-dispatch";
import { useEffect, useState } from "react";
import { demoEntries } from "./handlers.js";

const handler = createWorkstationDispatchHandler(demoEntries);

export function App() {
	const [ns, setNs] = useState<HttpFxtrReadNamespace | null>(null);

	useEffect(() => {
		let cancelled = false;

		async function init() {
			const namespace = await createHttpReadNamespace((...args) => fetch(...args));
			// Warm the scope by retrieving each pin so subsequent loads
			// through the namespace panel don't fail with "entity not
			// found in any parent scope".
			const allPins = await namespace.listPins();
			for (const [name] of allPins) {
				await namespace.retrieve(name);
			}
			if (!cancelled) setNs(namespace);
		}

		init().catch(console.error);
		// TODO: this cleanup leaks ns.scope (see step-3 App comment).
		// Not urgent for a dev demo; fix alongside fxtr-react StrictMode
		// handling.
		return () => {
			cancelled = true;
		};
	}, []);

	if (!ns) {
		return <div style={{ padding: "16px", fontSize: "14px" }}>Loading workstation demo…</div>;
	}

	return (
		<FxtrProvider scope={ns.scope}>
			<DispatchProvider handler={handler}>
				<div
					style={{
						height: "100%",
						display: "flex",
						flexDirection: "column",
						fontFamily: "var(--font-prose)",
						color: "var(--text-primary)",
					}}
				>
					<header
						style={{
							padding: "var(--sp-3) var(--sp-4)",
							borderBottom: "1px solid var(--border-subtle)",
							background: "var(--surface-raised)",
							flex: "0 0 auto",
						}}
					>
						<strong style={{ fontSize: "var(--text-body)", color: "var(--text-primary)" }}>
							fxtr-workstation dev demo
						</strong>
					</header>
					<div style={{ flex: "1 1 auto", minHeight: 0 }}>
						<WorkstationShell namespace={ns} tryRetrieveById={(id) => ns.tryRetrieveById(id)} />
					</div>
				</div>
			</DispatchProvider>
		</FxtrProvider>
	);
}
