/**
 * React entry. Imports dockview's own stylesheet + the
 * workstation theme bundle, mounts `<App />` under StrictMode.
 */

import "dockview/dist/styles/dockview.css";
import "@fxtr-ui/panel-viewer/styles/all.css";

import React from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";

const root = document.getElementById("root");
if (!root) throw new Error("missing #root");
createRoot(root).render(
	<React.StrictMode>
		<App />
	</React.StrictMode>,
);
