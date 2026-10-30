/**
 * Fingerprint report client: connects to the fxtr HTTP server, discovers
 * the report via pins, sets up dispatch + report shell.
 */

import type { FxtrID, FxtrScope } from "@fxtr/core";
import {
	type HttpFxtrReadNamespace,
	createHttpReadNamespace,
	withBaseUrl,
} from "@fxtr/http-client";
import { FxtrProvider } from "@fxtr/react";
import { DispatchProvider } from "@fxtr/react-dispatch";
import { ReportShell, type ReportView } from "@fxtr/reports-react";
import { useEffect, useMemo, useState } from "react";
import { createFingerprintDispatchHandler } from "./registry.js";

declare global {
	interface Window {
		__FXTR_API_BASE_URL__?: string;
	}
}

const handler = createFingerprintDispatchHandler();

export function App() {
	const [state, setState] = useState<{
		ns: HttpFxtrReadNamespace | null;
		reportIds: FxtrID[];
	}>({ ns: null, reportIds: [] });

	useEffect(() => {
		let cancelled = false;

		async function init() {
			const baseUrl = window.__FXTR_API_BASE_URL__;
			const fetchFn = baseUrl
				? withBaseUrl(baseUrl)
				: (...args: Parameters<typeof fetch>) => fetch(...args);
			const ns = await createHttpReadNamespace(fetchFn);

			const allPins = await ns.listPins();
			const reportIds: FxtrID[] = [];

			for (const [name] of allPins) {
				const id = await ns.retrieve(name);
				if (name.startsWith("reports/")) {
					reportIds.push(id);
				}
			}

			if (!cancelled) {
				setState({ ns, reportIds });
			}
		}

		init().catch(console.error);
		return () => {
			cancelled = true;
		};
	}, []);

	if (!state.ns) {
		return <div style={{ padding: "24px", fontSize: "16px" }}>Loading fingerprint report...</div>;
	}

	return (
		<FxtrProvider scope={state.ns.scope}>
			<DispatchProvider handler={handler}>
				<ReportApp ns={state.ns} reportIds={state.reportIds} scope={state.ns.scope} />
			</DispatchProvider>
		</FxtrProvider>
	);
}

function ReportApp({
	ns,
	reportIds,
}: {
	ns: HttpFxtrReadNamespace;
	reportIds: FxtrID[];
	scope: FxtrScope;
}) {
	const initialView: ReportView = useMemo(
		() => ({ kind: "cards" as const, entities: reportIds }),
		[reportIds],
	);

	const syncUrl = useMemo(
		() => ({ tryRetrieveById: (id: FxtrID) => ns.tryRetrieveById(id) }),
		[ns],
	);

	return <ReportShell initialView={initialView} syncUrl={syncUrl} />;
}
