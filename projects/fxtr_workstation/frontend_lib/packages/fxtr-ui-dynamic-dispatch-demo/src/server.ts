/**
 * Dynamic-dispatch-demo orchestrator. Spawns the Python
 * workstation backend, reads its stdout protocol lines, then
 * starts Vite with the connection info injected as
 * `import.meta.env` vars:
 *
 *   - `VITE_WORKSTATION_URL` — the `/client` WebSocket URL.
 *   - `VITE_WORKSTATION_DEBUG_UI_URL` — optional debug UI URL.
 *   - `VITE_RENDERER_DISPATCHER_NAME` — the external name the
 *     backend mounted the renderer-dispatcher service under.
 *   - `VITE_DYNAMIC_DISPATCH_TYPE_IDS` — JSON dict; not strictly
 *     needed by the client (lookups happen by short_id at runtime)
 *     but emitted for debugging parity with `dispatch-demo`.
 *
 * Same shape as the sibling `@fxtr-ui/dispatch-demo`
 * orchestrator; the `RENDERER_DISPATCHER_NAME` line is the new
 * piece specific to the dynamic dispatcher.
 */

import { type ChildProcess, spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createServer as createViteServer } from "vite";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PY_SCRIPT = path.resolve(__dirname, "dynamic_dispatch_demo_workstation.py");
const READY_TIMEOUT_MS = 30_000;
const TERM_TIMEOUT_MS = 5_000;

interface WorkstationReady {
	readonly baseUrl: string;
	readonly debugUiUrl: string | null;
	readonly typeIdsJson: string;
	readonly rendererDispatcherName: string;
}

async function main(): Promise<void> {
	const child = spawn("uv", ["run", "python", PY_SCRIPT], {
		stdio: ["ignore", "pipe", "pipe"],
		cwd: __dirname,
	});

	const stderrLines: string[] = [];
	child.stderr?.on("data", (chunk: Buffer) => {
		const text = chunk.toString();
		stderrLines.push(text);
		process.stderr.write(`[dyn-dispatch-demo-workstation] ${text}`);
	});

	let ready: WorkstationReady;
	try {
		ready = await readReadyLines(child);
	} catch (err) {
		console.error("[dyn-dispatch-demo] failed to start workstation:", err);
		console.error("[dyn-dispatch-demo] workstation stderr:\n", stderrLines.join(""));
		child.kill("SIGTERM");
		process.exit(1);
	}

	const workstationClientUrl = `${ready.baseUrl}/client`;
	console.log(`[dyn-dispatch-demo] workstation ready: ${workstationClientUrl}`);
	if (ready.debugUiUrl) {
		console.log(`[dyn-dispatch-demo] workstation debug UI: ${ready.debugUiUrl}`);
	}
	console.log(`[dyn-dispatch-demo] renderer-dispatcher: ${ready.rendererDispatcherName}`);
	console.log(`[dyn-dispatch-demo] type-ids: ${ready.typeIdsJson}`);

	process.env.VITE_WORKSTATION_URL = workstationClientUrl;
	if (ready.debugUiUrl) {
		process.env.VITE_WORKSTATION_DEBUG_UI_URL = ready.debugUiUrl;
	}
	process.env.VITE_RENDERER_DISPATCHER_NAME = ready.rendererDispatcherName;
	process.env.VITE_DYNAMIC_DISPATCH_TYPE_IDS = ready.typeIdsJson;

	const vite = await createViteServer();
	await vite.listen();
	vite.printUrls();

	let shuttingDown = false;
	const shutdown = async (signal: NodeJS.Signals): Promise<void> => {
		if (shuttingDown) return;
		shuttingDown = true;
		console.log(`\n[dyn-dispatch-demo] received ${signal}, shutting down`);
		try {
			await vite.close();
		} catch (err) {
			console.error("[dyn-dispatch-demo] error closing vite:", err);
		}
		await stopChild(child);
		process.exit(0);
	};
	process.on("SIGINT", () => void shutdown("SIGINT"));
	process.on("SIGTERM", () => void shutdown("SIGTERM"));

	child.on("exit", (code, sig) => {
		if (!shuttingDown) {
			console.error(
				`[dyn-dispatch-demo] workstation child exited (code=${code}, signal=${sig}); shutting down`,
			);
			void shutdown("SIGTERM");
		}
	});
}

function readReadyLines(child: ChildProcess): Promise<WorkstationReady> {
	return new Promise<WorkstationReady>((resolve, reject) => {
		let buf = "";
		let settled = false;
		let readyUrl: string | null = null;
		let debugUiUrl: string | null = null;
		let typeIdsJson: string | null = null;
		let rendererDispatcherName: string | null = null;

		const trySettle = (): void => {
			if (settled) return;
			if (readyUrl === null) return;
			if (typeIdsJson === null) {
				settled = true;
				cleanup();
				reject(new Error("workstation emitted READY without TYPE_IDS"));
				return;
			}
			if (rendererDispatcherName === null) {
				settled = true;
				cleanup();
				reject(new Error("workstation emitted READY without RENDERER_DISPATCHER_NAME"));
				return;
			}
			settled = true;
			cleanup();
			resolve({
				baseUrl: readyUrl,
				debugUiUrl,
				typeIdsJson,
				rendererDispatcherName,
			});
		};

		const onData = (chunk: Buffer): void => {
			buf += chunk.toString();
			let nl: number;
			// biome-ignore lint/suspicious/noAssignInExpressions: line-split idiom
			while ((nl = buf.indexOf("\n")) !== -1) {
				const line = buf.slice(0, nl).trim();
				buf = buf.slice(nl + 1);
				if (line.startsWith("READY ")) {
					readyUrl = line.slice("READY ".length);
					continue;
				}
				if (line.startsWith("TYPE_IDS ")) {
					typeIdsJson = line.slice("TYPE_IDS ".length);
					continue;
				}
				if (line.startsWith("DEBUG_UI ")) {
					debugUiUrl = line.slice("DEBUG_UI ".length);
					continue;
				}
				if (line.startsWith("RENDERER_DISPATCHER_NAME ")) {
					rendererDispatcherName = line.slice("RENDERER_DISPATCHER_NAME ".length);
					continue;
				}
				if (line.length > 0) {
					process.stdout.write(`[dyn-dispatch-demo-workstation] ${line}\n`);
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
	console.error("[dyn-dispatch-demo] fatal:", err);
	process.exit(1);
});
