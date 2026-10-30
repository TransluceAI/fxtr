/**
 * Spawn the `@fxtr-ui/dynamic-dispatch-demo` Python workstation
 * as a child process. Reads `TYPE_IDS`, `RENDERER_DISPATCHER_NAME`,
 * `READY`, and (optional) `DEBUG_UI` from stdout.
 *
 * Sibling of `_integration_harness.ts`. We can't share the harness
 * directly because the dynamic-dispatch demo emits an extra line
 * (`RENDERER_DISPATCHER_NAME`) that the static demo doesn't, and
 * the test ergonomics are different enough (compile-and-register
 * pre-amble in the Python backend takes time) that we want a
 * dedicated startup timeout.
 */

import { type ChildProcess, spawn } from "node:child_process";
import path from "node:path";

// Longer than the static dispatch demo's 15s — the dynamic demo's
// Python startup runs esbuild to compile the renderers, which adds
// ~5s on first run (npx fetch + esbuild bin) on cold environments.
const READY_TIMEOUT_MS = 60_000;
const TERM_TIMEOUT_MS = 5_000;

const DEMO_SCRIPT = path.resolve(
	__dirname,
	"..",
	"..",
	"fxtr-ui-dynamic-dispatch-demo",
	"src",
	"dynamic_dispatch_demo_workstation.py",
);

export interface DynamicDispatchDemoTypeIds {
	readonly note: string;
	readonly photo: string;
}

export interface DynamicDispatchDemoWorkstation {
	readonly clientUrl: string;
	readonly typeIds: DynamicDispatchDemoTypeIds;
	readonly rendererDispatcherName: string;
	close(): Promise<void>;
}

export async function startDynamicDispatchDemoWorkstation(): Promise<DynamicDispatchDemoWorkstation> {
	const child = spawn("uv", ["run", "python", DEMO_SCRIPT], {
		stdio: ["ignore", "pipe", "pipe"],
		cwd: path.dirname(DEMO_SCRIPT),
	});

	const stderrLines: string[] = [];
	child.stderr?.on("data", (chunk: Buffer) => {
		stderrLines.push(chunk.toString());
	});

	const ready = await readReadyLines(child);
	return {
		clientUrl: `${ready.baseUrl}/client`,
		typeIds: ready.typeIds,
		rendererDispatcherName: ready.rendererDispatcherName,
		close: () => stopChild(child, stderrLines),
	};
}

interface ReadyInfo {
	readonly baseUrl: string;
	readonly typeIds: DynamicDispatchDemoTypeIds;
	readonly rendererDispatcherName: string;
}

function readReadyLines(child: ChildProcess): Promise<ReadyInfo> {
	return new Promise<ReadyInfo>((resolve, reject) => {
		let buf = "";
		let settled = false;
		let baseUrl: string | null = null;
		let typeIds: DynamicDispatchDemoTypeIds | null = null;
		let rendererDispatcherName: string | null = null;

		const trySettle = (): void => {
			if (settled) return;
			if (baseUrl !== null && typeIds !== null && rendererDispatcherName !== null) {
				settled = true;
				cleanup();
				resolve({ baseUrl, typeIds, rendererDispatcherName });
			}
		};
		const settleErr = (err: Error): void => {
			if (settled) return;
			settled = true;
			cleanup();
			if (!child.killed) child.kill("SIGKILL");
			reject(err);
		};

		const onData = (chunk: Buffer): void => {
			buf += chunk.toString();
			let nl = buf.indexOf("\n");
			while (nl !== -1) {
				const line = buf.slice(0, nl).trim();
				buf = buf.slice(nl + 1);
				if (line.startsWith("READY ")) {
					baseUrl = line.slice("READY ".length);
				} else if (line.startsWith("TYPE_IDS ")) {
					try {
						typeIds = JSON.parse(line.slice("TYPE_IDS ".length)) as DynamicDispatchDemoTypeIds;
					} catch (err) {
						settleErr(new Error(`TYPE_IDS JSON parse failed: ${String(err)}`));
						return;
					}
				} else if (line.startsWith("RENDERER_DISPATCHER_NAME ")) {
					rendererDispatcherName = line.slice("RENDERER_DISPATCHER_NAME ".length);
				}
				nl = buf.indexOf("\n");
			}
			trySettle();
		};
		const onExit = (code: number | null, signal: NodeJS.Signals | null): void => {
			settleErr(new Error(`workstation exited before READY (code=${code}, signal=${signal})`));
		};
		const onError = (err: Error): void => {
			settleErr(new Error(`workstation spawn error: ${err.message}`));
		};
		const onTimeout = (): void => {
			settleErr(
				new Error(
					`workstation did not emit READY + TYPE_IDS + RENDERER_DISPATCHER_NAME within ${READY_TIMEOUT_MS}ms`,
				),
			);
		};

		const timer = setTimeout(onTimeout, READY_TIMEOUT_MS);
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

async function stopChild(child: ChildProcess, stderrLines: string[]): Promise<void> {
	if (child.exitCode !== null) return;
	const exited = new Promise<void>((resolve) => {
		const onExit = (): void => {
			clearTimeout(timer);
			resolve();
		};
		const timer = setTimeout(() => {
			child.off("exit", onExit);
			if (!child.killed) child.kill("SIGKILL");
			if (stderrLines.length) {
				process.stderr.write(`[dyn-dispatch-demo-workstation stderr]\n${stderrLines.join("")}`);
			}
			resolve();
		}, TERM_TIMEOUT_MS);
		child.once("exit", onExit);
		if (!child.killed) child.kill("SIGTERM");
	});
	await exited;
}
