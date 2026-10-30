/**
 * Demo orchestrator: spawns the Python workstation, reads its
 * `READY <ws-url>` line, then starts Vite's dev server with the URL
 * injected as `import.meta.env.VITE_WORKSTATION_URL`. Tears down the
 * Python child on SIGINT/SIGTERM.
 *
 * Pattern mirrors the integration tests' `_integration_harness.ts`,
 * extended with a Vite-dev front-end half. Run via `pnpm dev` from
 * this package (or `tsx src/server.ts` directly).
 *
 * The Python script (`src/demo_workstation.py`) is invoked via `uv
 * run python ...`; `uv` walks up the directory tree to find the
 * workstation Python package's pyproject.toml.
 */

import { type ChildProcess, spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createServer as createViteServer } from "vite";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PY_SCRIPT = path.resolve(__dirname, "demo_workstation.py");
const READY_TIMEOUT_MS = 15_000;
const TERM_TIMEOUT_MS = 5_000;

async function main(): Promise<void> {
	const child = spawn("uv", ["run", "python", PY_SCRIPT], {
		stdio: ["ignore", "pipe", "pipe"],
		cwd: __dirname,
	});

	// Mirror stderr so demo-runtime errors are visible.
	const stderrLines: string[] = [];
	child.stderr?.on("data", (chunk: Buffer) => {
		const text = chunk.toString();
		stderrLines.push(text);
		process.stderr.write(`[demo-workstation] ${text}`);
	});

	let ready: { baseUrl: string; debugUiUrl: string | null };
	try {
		ready = await readReadyLines(child);
	} catch (err) {
		console.error("[demo] failed to start workstation:", err);
		console.error("[demo] workstation stderr:\n", stderrLines.join(""));
		child.kill("SIGTERM");
		process.exit(1);
	}

	const workstationClientUrl = `${ready.baseUrl}/client`;
	console.log(`[demo] workstation ready: ${workstationClientUrl}`);
	if (ready.debugUiUrl) {
		console.log(`[demo] workstation debug UI: ${ready.debugUiUrl}`);
	}

	// Vite picks up `VITE_*` from process.env automatically and
	// exposes them on `import.meta.env`.
	process.env.VITE_WORKSTATION_URL = workstationClientUrl;
	if (ready.debugUiUrl) {
		process.env.VITE_WORKSTATION_DEBUG_UI_URL = ready.debugUiUrl;
	}

	const vite = await createViteServer();
	await vite.listen();
	vite.printUrls();

	let shuttingDown = false;
	const shutdown = async (signal: NodeJS.Signals): Promise<void> => {
		if (shuttingDown) return;
		shuttingDown = true;
		console.log(`\n[demo] received ${signal}, shutting down`);
		try {
			await vite.close();
		} catch (err) {
			console.error("[demo] error closing vite:", err);
		}
		await stopChild(child);
		process.exit(0);
	};
	process.on("SIGINT", () => void shutdown("SIGINT"));
	process.on("SIGTERM", () => void shutdown("SIGTERM"));

	// If the workstation child dies on its own, tear down too.
	child.on("exit", (code, sig) => {
		if (!shuttingDown) {
			console.error(`[demo] workstation child exited (code=${code}, signal=${sig}); shutting down`);
			void shutdown("SIGTERM");
		}
	});
}

/**
 * Read the child's stdout line-by-line until we see `READY <url>`,
 * also capturing the optional follow-up `DEBUG_UI <url>` line. The
 * Python script prints DEBUG_UI right after READY when the bundled
 * debug UI is mounted; we settle as soon as we see one or the other
 * READY-and-buffer-has-no-DEBUG_UI-yet — concretely, we resolve once
 * READY arrives and then wait briefly for DEBUG_UI before resolving.
 *
 * Simpler approach: resolve on READY, but synchronously check the
 * already-buffered next line. DEBUG_UI follows READY immediately on
 * the Python side (both `print(..., flush=True)`), so a single
 * data-event usually contains both.
 */
function readReadyLines(
	child: ChildProcess,
): Promise<{ baseUrl: string; debugUiUrl: string | null }> {
	return new Promise<{ baseUrl: string; debugUiUrl: string | null }>((resolve, reject) => {
		let buf = "";
		let settled = false;
		let readyUrl: string | null = null;
		let debugUiUrl: string | null = null;

		const trySettle = (): void => {
			if (settled) return;
			if (readyUrl === null) return;
			// Settle on READY; DEBUG_UI is optional / best-effort.
			// (If it lands within the same data event we'll have already
			// captured it before this fires.)
			settled = true;
			cleanup();
			resolve({ baseUrl: readyUrl, debugUiUrl });
		};

		const onData = (chunk: Buffer): void => {
			buf += chunk.toString();
			let nl: number;
			// biome-ignore lint/suspicious/noAssignInExpressions: line-splitting idiom
			while ((nl = buf.indexOf("\n")) !== -1) {
				const line = buf.slice(0, nl).trim();
				buf = buf.slice(nl + 1);
				if (line.startsWith("READY ")) {
					readyUrl = line.slice("READY ".length);
					continue;
				}
				if (line.startsWith("DEBUG_UI ")) {
					debugUiUrl = line.slice("DEBUG_UI ".length);
					continue;
				}
				if (line.length > 0) {
					process.stdout.write(`[demo-workstation] ${line}\n`);
				}
			}
			trySettle();
		};
		const onExit = (code: number | null, sig: NodeJS.Signals | null): void => {
			if (settled) return;
			settled = true;
			cleanup();
			reject(new Error(`workstation exited before READY (code=${code}, signal=${sig})`));
		};
		const onError = (err: Error): void => {
			if (settled) return;
			settled = true;
			cleanup();
			reject(err);
		};
		const timer = setTimeout(() => {
			if (settled) return;
			settled = true;
			cleanup();
			reject(new Error(`workstation did not emit READY within ${READY_TIMEOUT_MS}ms`));
		}, READY_TIMEOUT_MS);

		const cleanup = (): void => {
			clearTimeout(timer);
			child.stdout?.off("data", onData);
			child.off("exit", onExit);
			child.off("error", onError);
		};

		child.stdout?.on("data", onData);
		child.on("exit", onExit);
		child.on("error", onError);
	});
}

async function stopChild(child: ChildProcess): Promise<void> {
	if (child.exitCode !== null) return;
	return new Promise<void>((resolve) => {
		const onExit = (): void => {
			clearTimeout(timer);
			resolve();
		};
		const timer = setTimeout(() => {
			child.off("exit", onExit);
			child.kill("SIGKILL");
			resolve();
		}, TERM_TIMEOUT_MS);
		child.once("exit", onExit);
		child.kill("SIGTERM");
	});
}

void main().catch((err) => {
	console.error("[demo] fatal:", err);
	process.exit(1);
});
