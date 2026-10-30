/**
 * Vite entry for the dev demo client. Side-effect CSS imports happen here
 * (workstation-core is intentionally free of CSS imports so library
 * consumers control stylesheet order).
 */

import "dockview/dist/styles/dockview.css";
import "@fxtr-workstation/theme/all.css";

import { createRoot } from "react-dom/client";
import { App } from "./App.js";

const rootEl = document.getElementById("root");
if (!rootEl) {
	throw new Error("Missing #root container in index.html");
}

// NOTE: No <StrictMode>. FxtrProvider's scope lifecycle doesn't survive
// StrictMode's mount→unmount→remount dance today — the parent scope gets
// scheduleClose()'d on fake-unmount but the render that would recreate it
// doesn't re-run, so child <Render>s try to include a closed scope. The
// fingerprinting-report demo sidesteps the same way. Revisit if we want
// StrictMode in this repo; likely needs FxtrProvider to keep the scope
// alive across fast unmount/remount or to re-create on demand.
createRoot(rootEl).render(<App />);
