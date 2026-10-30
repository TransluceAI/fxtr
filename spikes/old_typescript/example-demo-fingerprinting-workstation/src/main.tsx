import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

const rootEl = document.getElementById("root");
if (!rootEl) throw new Error("missing #root");

createRoot(rootEl).render(
	<StrictMode>
		<div>fxtr-workstation demo — scaffolding only</div>
	</StrictMode>,
);
