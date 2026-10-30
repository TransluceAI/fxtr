/**
 * fxtr_http_demo orchestrator. Spawns the Python stateless backend
 * (`../server/demo_backend.py`, via `uv run` against this example's
 * pyproject), reads its `READY <http-base-url>` line, then starts Vite's
 * dev server with the base URL injected as `import.meta.env`:
 *
 *   - `VITE_HTTP_BACKEND_URL` — the backend's http base URL. The web app
 *     constructs `new HttpConnector({ baseUrl })` from it; discovery,
 *     `/data`, REST services, and the (relative) ws descriptor all
 *     resolve against it.
 *
 * Same shape as `@fxtr-ui/workstation-demo`'s orchestrator; the backend
 * is the stateless HTTP app rather than a workstation server.
 */

import { type ChildProcess, spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createServer as createViteServer } from "vite";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
// `web/src` → `web` → `examples/fxtr_http_demo` (the uv project root).
const PROJECT_ROOT = path.resolve(__dirname, "..", "..");
const PY_SCRIPT = "server/demo_backend.py";
const READY_TIMEOUT_MS = 60_000; // first `uv run` may sync the env
const TERM_TIMEOUT_MS = 5_000;

async function main(): Promise<void> {
	const child = spawn("uv", ["run", "python", PY_SCRIPT], {
		stdio: ["ignore", "pipe", "pipe"],
		cwd: PROJECT_ROOT,
	});

	const stderrLines: string[] = [];
	child.stderr?.on("data", (chunk: Buffer) => {
		const text = chunk.toString();
		stderrLines.push(text);
		process.stderr.write(`[http-demo-backend] ${text}`);
	});

	let baseUrl: string;
	try {
		baseUrl = await readReadyLine(child);
	} catch (err) {
		console.error("[http-demo] failed to start backend:", err);
		console.error("[http-demo] backend stderr:\n", stderrLines.join(""));
		child.kill("SIGTERM");
		process.exit(1);
	}

	console.log(`[http-demo] backend ready: ${baseUrl}`);
	process.env.VITE_HTTP_BACKEND_URL = baseUrl;

	const vite = await createViteServer();
	await vite.listen();
	vite.printUrls();

	let shuttingDown = false;
	const shutdown = async (signal: NodeJS.Signals): Promise<void> => {
		if (shuttingDown) return;
		shuttingDown = true;
		console.log(`\n[http-demo] received ${signal}, shutting down`);
		try {
			await vite.close();
		} catch (err) {
			console.error("[http-demo] error closing vite:", err);
		}
		await stopChild(child);
		process.exit(0);
	};
	process.on("SIGINT", () => void shutdown("SIGINT"));
	process.on("SIGTERM", () => void shutdown("SIGTERM"));

	child.on("exit", (code, sig) => {
		if (!shuttingDown) {
			console.error(`[http-demo] backend exited (code=${code}, signal=${sig}); shutting down`);
			void shutdown("SIGTERM");
		}
	});
}

/** Read the child's stdout line-by-line, settling on `READY <url>`. */
function readReadyLine(child: ChildProcess): Promise<string> {
	return new Promise<string>((resolve, reject) => {
		let buf = "";
		let settled = false;

		const onData = (chunk: Buffer): void => {
			buf += chunk.toString();
			let nl: number;
			// biome-ignore lint/suspicious/noAssignInExpressions: line-splitting idiom
			while ((nl = buf.indexOf("\n")) !== -1) {
				const line = buf.slice(0, nl).trim();
				buf = buf.slice(nl + 1);
				if (line.startsWith("READY ")) {
					settled = true;
					cleanup();
					resolve(line.slice("READY ".length));
					return;
				}
				if (line.length > 0) {
					process.stdout.write(`[http-demo-backend] ${line}\n`);
				}
			}
		};
		const onExit = (code: number | null, sig: NodeJS.Signals | null): void => {
			if (settled) return;
			settled = true;
			cleanup();
			reject(new Error(`backend exited before READY (code=${code}, signal=${sig})`));
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
			reject(new Error(`backend did not emit READY within ${READY_TIMEOUT_MS}ms`));
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
	console.error("[http-demo] fatal:", err);
	process.exit(1);
});
