/**
 * Spawn the `@fxtr-ui/dispatch-demo` Python workstation as a
 * child process for the integration tests. Reads `TYPE_IDS`,
 * `READY`, and (optional) `DEBUG_UI` from stdout.
 *
 * The Python script lives at
 * `../../fxtr-ui-dispatch-demo/src/dispatch_demo_workstation.py`
 * and is invoked via `uv run python ...`. uv walks up from the
 * script's directory to find the workstation Python package's
 * pyproject.toml.
 */

import { type ChildProcess, spawn } from "node:child_process";
import path from "node:path";

const READY_TIMEOUT_MS = 15_000;
const TERM_TIMEOUT_MS = 5_000;

const DEMO_SCRIPT = path.resolve(
	__dirname,
	"..",
	"..",
	"fxtr-ui-dispatch-demo",
	"src",
	"dispatch_demo_workstation.py",
);

export interface DispatchDemoTypeIds {
	readonly note: string;
	readonly photo: string;
	readonly echo: string;
	readonly counter: string;
}

export interface DispatchDemoWorkstation {
	readonly clientUrl: string;
	readonly typeIds: DispatchDemoTypeIds;
	close(): Promise<void>;
}

export async function startDispatchDemoWorkstation(): Promise<DispatchDemoWorkstation> {
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
		close: () => stopChild(child, stderrLines),
	};
}

interface ReadyInfo {
	readonly baseUrl: string;
	readonly typeIds: DispatchDemoTypeIds;
}

function readReadyLines(child: ChildProcess): Promise<ReadyInfo> {
	return new Promise<ReadyInfo>((resolve, reject) => {
		let buf = "";
		let settled = false;
		let baseUrl: string | null = null;
		let typeIds: DispatchDemoTypeIds | null = null;

		const trySettle = (): void => {
			if (settled) return;
			if (baseUrl !== null && typeIds !== null) {
				settled = true;
				cleanup();
				resolve({ baseUrl, typeIds });
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
						typeIds = JSON.parse(line.slice("TYPE_IDS ".length)) as DispatchDemoTypeIds;
					} catch (err) {
						settleErr(new Error(`TYPE_IDS JSON parse failed: ${String(err)}`));
						return;
					}
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
			settleErr(new Error(`workstation did not emit READY+TYPE_IDS within ${READY_TIMEOUT_MS}ms`));
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
			// Surface stderr if SIGTERM didn't take.
			if (stderrLines.length) {
				process.stderr.write(`[dispatch-demo-workstation stderr]\n${stderrLines.join("")}`);
			}
			resolve();
		}, TERM_TIMEOUT_MS);
		child.once("exit", onExit);
		if (!child.killed) child.kill("SIGTERM");
	});
	await exited;
}
