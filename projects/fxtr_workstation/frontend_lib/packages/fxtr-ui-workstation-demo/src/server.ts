/**
 * Workstation-demo orchestrator. Spawns the Python workstation,
 * reads its `TYPE_IDS <json>`, `READY <ws-url>`, and optional
 * `DEBUG_UI <http-url>` lines, then starts Vite's dev server
 * with those values injected as `import.meta.env`:
 *
 *   - `VITE_WORKSTATION_URL` — the `/client` endpoint.
 *   - `VITE_WORKSTATION_DEBUG_UI_URL` — optional.
 *   - `VITE_DEMO_TYPE_IDS` — JSON dict mapping label
 *     (`note`, `list_node`, `echo`, `counter`) to the type
 *     entity's id-string. The TS-side dispatch registry uses
 *     these as lookup-map keys.
 *
 * Same shape as the sibling `@fxtr-ui/dispatch-demo` orchestrator;
 * env-var name and log prefixes differ.
 */

import { type ChildProcess, spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createServer as createViteServer } from "vite";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PY_SCRIPT = path.resolve(__dirname, "demo_workstation.py");
const READY_TIMEOUT_MS = 15_000;
const TERM_TIMEOUT_MS = 5_000;

interface WorkstationReady {
	readonly baseUrl: string;
	readonly debugUiUrl: string | null;
	readonly typeIdsJson: string;
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
		process.stderr.write(`[workstation-demo-backend] ${text}`);
	});

	let ready: WorkstationReady;
	try {
		ready = await readReadyLines(child);
	} catch (err) {
		console.error("[workstation-demo] failed to start workstation:", err);
		console.error("[workstation-demo] workstation stderr:\n", stderrLines.join(""));
		child.kill("SIGTERM");
		process.exit(1);
	}

	const workstationClientUrl = `${ready.baseUrl}/client`;
	console.log(`[workstation-demo] workstation ready: ${workstationClientUrl}`);
	if (ready.debugUiUrl) {
		console.log(`[workstation-demo] workstation debug UI: ${ready.debugUiUrl}`);
	}
	console.log(`[workstation-demo] demo type-ids: ${ready.typeIdsJson}`);

	process.env.VITE_WORKSTATION_URL = workstationClientUrl;
	if (ready.debugUiUrl) {
		process.env.VITE_WORKSTATION_DEBUG_UI_URL = ready.debugUiUrl;
	}
	process.env.VITE_DEMO_TYPE_IDS = ready.typeIdsJson;

	const vite = await createViteServer();
	await vite.listen();
	vite.printUrls();

	let shuttingDown = false;
	const shutdown = async (signal: NodeJS.Signals): Promise<void> => {
		if (shuttingDown) return;
		shuttingDown = true;
		console.log(`\n[workstation-demo] received ${signal}, shutting down`);
		try {
			await vite.close();
		} catch (err) {
			console.error("[workstation-demo] error closing vite:", err);
		}
		await stopChild(child);
		process.exit(0);
	};
	process.on("SIGINT", () => void shutdown("SIGINT"));
	process.on("SIGTERM", () => void shutdown("SIGTERM"));

	child.on("exit", (code, sig) => {
		if (!shuttingDown) {
			console.error(
				`[workstation-demo] workstation child exited (code=${code}, signal=${sig}); shutting down`,
			);
			void shutdown("SIGTERM");
		}
	});
}

/**
 * Read the child's stdout line-by-line, settling once `READY`
 * has arrived. `TYPE_IDS` is required (the demo can't build its
 * dispatch registry without it); `DEBUG_UI` is optional. The
 * Python side prints in the order TYPE_IDS, READY, DEBUG_UI —
 * we settle on READY but also wait for TYPE_IDS to have been
 * seen.
 */
function readReadyLines(child: ChildProcess): Promise<WorkstationReady> {
	return new Promise<WorkstationReady>((resolve, reject) => {
		let buf = "";
		let settled = false;
		let readyUrl: string | null = null;
		let debugUiUrl: string | null = null;
		let typeIdsJson: string | null = null;

		const trySettle = (): void => {
			if (settled) return;
			if (readyUrl === null) return;
			if (typeIdsJson === null) {
				// READY arrived but no TYPE_IDS yet — fail fast rather
				// than start Vite without the dispatch type-id map.
				settled = true;
				cleanup();
				reject(new Error("workstation emitted READY without TYPE_IDS"));
				return;
			}
			settled = true;
			cleanup();
			resolve({ baseUrl: readyUrl, debugUiUrl, typeIdsJson });
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
				if (line.startsWith("TYPE_IDS ")) {
					typeIdsJson = line.slice("TYPE_IDS ".length);
					continue;
				}
				if (line.startsWith("DEBUG_UI ")) {
					debugUiUrl = line.slice("DEBUG_UI ".length);
					continue;
				}
				if (line.length > 0) {
					process.stdout.write(`[workstation-demo-backend] ${line}\n`);
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
	console.error("[workstation-demo] fatal:", err);
	process.exit(1);
});
