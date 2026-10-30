import { createRoot } from "react-dom/client";
import { App } from "./App.js";

// biome-ignore lint/style/noNonNullAssertion: root element always present in index.html
createRoot(document.getElementById("root")!).render(<App />);
