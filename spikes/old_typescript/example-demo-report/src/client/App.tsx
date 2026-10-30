/**
 * Demo report client: connects to the fxtr HTTP server, discovers reports
 * and comments via pins, sets up dispatch + report shell.
 */

import { Eager, EntityRef, type FxtrID, type FxtrScope, type PromiseOrEager } from "@fxtr/core";
import { type HttpFxtrReadNamespace, createHttpReadNamespace } from "@fxtr/http-client";
import { FxtrProvider } from "@fxtr/react";
import { DispatchProvider } from "@fxtr/react-dispatch";
import { SimpleComment } from "@fxtr/reports-entities";
import { ReportShell, type ReportView } from "@fxtr/reports-react";
import { createReportDispatchHandler } from "@fxtr/reports-react-handlers";
import { useEffect, useMemo, useState } from "react";

const handler = createReportDispatchHandler();

declare global {
	interface Window {
		__DEMO_CONFIG__?: { singleEntity?: boolean };
	}
}

export function App() {
	const [state, setState] = useState<{
		ns: HttpFxtrReadNamespace | null;
		reportIds: FxtrID[];
		commentIds: FxtrID[];
	}>({ ns: null, reportIds: [], commentIds: [] });

	useEffect(() => {
		let cancelled = false;

		async function init() {
			const ns = await createHttpReadNamespace((...args) => fetch(...args));

			// Discover reports and comments via pins, using retrieve to
			// fetch entity data into the scope
			const allPins = await ns.listPins();
			const reportIds: FxtrID[] = [];
			const commentIds: FxtrID[] = [];

			for (const [name] of allPins) {
				const id = await ns.retrieve(name);
				if (name.startsWith("reports/")) {
					reportIds.push(id);
				} else if (name.startsWith("comments/")) {
					commentIds.push(id);
				}
			}

			if (!cancelled) {
				setState({ ns, reportIds, commentIds });
			}
		}

		init().catch(console.error);
		return () => {
			cancelled = true;
		};
	}, []);

	if (!state.ns) {
		return <div style={{ padding: "24px", fontSize: "16px" }}>Loading...</div>;
	}

	return (
		<FxtrProvider scope={state.ns.scope}>
			<DispatchProvider handler={handler}>
				<ReportApp
					ns={state.ns}
					reportIds={state.reportIds}
					commentIds={state.commentIds}
					scope={state.ns.scope}
				/>
			</DispatchProvider>
		</FxtrProvider>
	);
}

function ReportApp({
	ns,
	reportIds,
	commentIds,
	scope,
}: {
	ns: HttpFxtrReadNamespace;
	reportIds: FxtrID[];
	commentIds: FxtrID[];
	scope: FxtrScope;
}) {
	const initialView: ReportView = useMemo(() => {
		const first = reportIds[0];
		if (window.__DEMO_CONFIG__?.singleEntity && first !== undefined) {
			return { kind: "main" as const, rootEntity: first };
		}
		return { kind: "cards" as const, entities: reportIds };
	}, [reportIds]);

	// Annotation resolver: loads each comment and checks if its target matches.
	// Caches loaded comment targets so subsequent calls resolve synchronously.
	const annotationsFor = useMemo(() => {
		const targetCache = new Map<string, FxtrID>();

		return (entityId: FxtrID): PromiseOrEager<FxtrID[]> => {
			const matching: FxtrID[] = [];
			const pending: Promise<void>[] = [];

			for (const commentId of commentIds) {
				const cached = targetCache.get(commentId.key());
				if (cached) {
					if (cached.key() === entityId.key()) {
						matching.push(commentId);
					}
					continue;
				}

				// Load the comment to check its target
				const ref = new EntityRef(commentId, SimpleComment);
				const loadResult = scope.load(ref);

				if (loadResult instanceof Eager) {
					try {
						const comment = loadResult.unwrap();
						targetCache.set(commentId.key(), comment.target);
						if (comment.target.key() === entityId.key()) {
							matching.push(commentId);
						}
					} catch {
						// Skip comments that fail to load
					}
				} else {
					pending.push(
						loadResult.then(
							(comment) => {
								targetCache.set(commentId.key(), comment.target);
								if (comment.target.key() === entityId.key()) {
									matching.push(commentId);
								}
							},
							() => {
								// Skip comments that fail to load
							},
						),
					);
				}
			}

			if (pending.length === 0) {
				return Eager.resolve(matching);
			}

			return Promise.all(pending).then(() => matching);
		};
	}, [commentIds, scope]);

	const syncUrl = useMemo(
		() => ({ tryRetrieveById: (id: FxtrID) => ns.tryRetrieveById(id) }),
		[ns],
	);

	return (
		<ReportShell initialView={initialView} annotationsFor={annotationsFor} syncUrl={syncUrl} />
	);
}
