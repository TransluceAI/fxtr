/**
 * React entry point for the fxtr web client example.
 *
 * Creates an HTTP-backed namespace and renders the app.
 *
 * Usage:
 *   1. Start the server: pnpm --filter @fxtr/examples server
 *   2. Start the web client: pnpm --filter @fxtr/examples web
 *   3. Open http://localhost:5173
 */

import { createRoot } from "react-dom/client";
import { App } from "./App.js";

const rootEl = document.getElementById("root");
if (!rootEl) throw new Error("Missing #root element");

createRoot(rootEl).render(<App />);
