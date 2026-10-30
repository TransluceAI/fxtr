import type { EntityRef, UnknownEntity } from "@fxtr/core";
import { useFxtrLoad } from "@fxtr/react";
import { useEffect } from "react";
import { JsonTree } from "../components/JsonTree.js";
import type { FullPanelProps } from "../keys.js";
import { shortId } from "../shortId.js";

export interface FullPanelFallbackProps extends FullPanelProps {
	entityRef: EntityRef<UnknownEntity>;
}

export function FullPanelFallback(props: FullPanelFallbackProps) {
	const { entityRef, setPanelTitle } = props;
	const unknown = useFxtrLoad(entityRef);

	const title = shortId(entityRef.entityId);
	// Effect-based: we don't know the entity value until after load,
	// so call the setter in an effect rather than during render.
	useEffect(() => {
		setPanelTitle(title);
	}, [setPanelTitle, title]);

	return (
		<div className="fw-panel__body" style={{ padding: "var(--sp-3, 12px)", overflow: "auto" }}>
			<JsonTree value={unknown.raw_cbor} />
		</div>
	);
}
