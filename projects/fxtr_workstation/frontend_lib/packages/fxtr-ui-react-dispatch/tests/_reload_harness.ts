/**
 * TypeScript wrapper around the Python reload-harness backend
 * (``_reload_harness_workstation.py``).
 *
 * Spawns the harness as a child process, reads the protocol
 * lines from stdout, and exposes a `triggerReload()` method that
 * writes a `TRIGGER_RELOAD` command to the harness's stdin and
 * awaits the matching `OK TRIGGER_RELOAD` reply.
 *
 * No mtime polling — the harness is purely command-driven so
 * tests don't rely on timing.
 */

import { spawn } from "node:child_process";
import path from "node:path";

const READY_TIMEOUT_MS = 60_000;
const TRIGGER_TIMEOUT_MS = 5_000;
const TERM_TIMEOUT_MS = 5_000;

const HARNESS_SCRIPT = path.resolve(__dirname, "_reload_harness_workstation.py");

export interface ReloadHarnessTypeIds {
	readonly note: string;
	readonly photo: string;
}

/** Maps externally-exposed entity name → renderer-template label. */
export interface ReloadHarnessEntityNames {
	readonly "harness-note": "note";
	readonly "harness-photo": "photo";
}

export interface ReloadHarnessWorkstation {
	readonly clientUrl: string;
	readonly typeIds: ReloadHarnessTypeIds;
	readonly rendererDispatcherName: string;
	readonly entityNames: ReloadHarnessEntityNames;
	/**
	 * Send `TRIGGER_RELOAD` to the harness. Resolves once the
	 * harness echoes `OK TRIGGER_RELOAD` (which it does immediately
	 * after calling `impl.trigger_reload()`).
	 */
	triggerReload(): Promise<void>;
	close(): Promise<void>;
}

export async function startReloadHarnessWorkstation(): Promise<ReloadHarnessWorkstation> {
	const child = spawn("uv", ["run", "python", HARNESS_SCRIPT], {
		stdio: ["pipe", "pipe", "pipe"],
		cwd: path.dirname(HARNESS_SCRIPT),
	});

	const stderrLines: string[] = [];
	child.stderr?.on("data", (chunk: Buffer) => {
		stderrLines.push(chunk.toString());
	});

	// Lines we haven't consumed yet — both the READY phase and the
	// `triggerReload()` response loop pull from this buffer.
	const stdoutQueue: string[] = [];
	const stdoutListeners: Array<(line: string) => void> = [];
	let stdoutBuf = "";

	child.stdout?.on("data", (chunk: Buffer) => {
		stdoutBuf += chunk.toString();
		let nl = stdoutBuf.indexOf("\n");
		while (nl !== -1) {
			const line = stdoutBuf.slice(0, nl).trim();
			stdoutBuf = stdoutBuf.slice(nl + 1);
			if (stdoutListeners.length > 0) {
				const listener = stdoutListeners.shift();
				listener?.(line);
			} else {
				stdoutQueue.push(line);
			}
			nl = stdoutBuf.indexOf("\n");
		}
	});

	function nextStdoutLine(timeoutMs: number, what: string): Promise<string> {
		return new Promise<string>((resolve, reject) => {
			if (stdoutQueue.length > 0) {
				const line = stdoutQueue.shift();
				if (line !== undefined) {
					resolve(line);
					return;
				}
			}
			const timer = setTimeout(() => {
				const idx = stdoutListeners.indexOf(listener);
				if (idx >= 0) stdoutListeners.splice(idx, 1);
				reject(new Error(`harness stdout: timed out waiting for ${what} after ${timeoutMs}ms`));
			}, timeoutMs);
			const listener = (line: string): void => {
				clearTimeout(timer);
				resolve(line);
			};
			stdoutListeners.push(listener);
		});
	}

	// Read protocol lines until READY arrives.
	const ready = await new Promise<{
		baseUrl: string;
		typeIds: ReloadHarnessTypeIds;
		rendererDispatcherName: string;
		entityNames: ReloadHarnessEntityNames;
	}>((resolve, reject) => {
		let baseUrl: string | null = null;
		let typeIds: ReloadHarnessTypeIds | null = null;
		let rendererDispatcherName: string | null = null;
		let entityNames: ReloadHarnessEntityNames | null = null;

		const timer = setTimeout(() => {
			settleErr(new Error(`harness did not emit READY within ${READY_TIMEOUT_MS}ms`));
		}, READY_TIMEOUT_MS);

		const trySettle = (): boolean => {
			if (
				baseUrl !== null &&
				typeIds !== null &&
				rendererDispatcherName !== null &&
				entityNames !== null
			) {
				clearTimeout(timer);
				resolve({ baseUrl, typeIds, rendererDispatcherName, entityNames });
				return true;
			}
			return false;
		};
		const settleErr = (err: Error): void => {
			clearTimeout(timer);
			if (!child.killed) child.kill("SIGKILL");
			reject(err);
		};

		const pump = (): void => {
			(async () => {
				while (!trySettle()) {
					const line = await nextStdoutLine(READY_TIMEOUT_MS, "harness READY phase line");
					if (line.startsWith("READY ")) {
						baseUrl = line.slice("READY ".length);
					} else if (line.startsWith("TYPE_IDS ")) {
						typeIds = JSON.parse(line.slice("TYPE_IDS ".length)) as ReloadHarnessTypeIds;
					} else if (line.startsWith("RENDERER_DISPATCHER_NAME ")) {
						rendererDispatcherName = line.slice("RENDERER_DISPATCHER_NAME ".length);
					} else if (line.startsWith("ENTITY_NAMES ")) {
						entityNames = JSON.parse(
							line.slice("ENTITY_NAMES ".length),
						) as ReloadHarnessEntityNames;
					}
				}
			})().catch(settleErr);
		};

		child.on("exit", (code, signal) => {
			settleErr(new Error(`harness exited before READY (code=${code}, signal=${signal})`));
		});
		pump();
	});

	const triggerReload = async (): Promise<void> => {
		if (child.exitCode !== null) {
			throw new Error("harness has exited; cannot trigger reload");
		}
		child.stdin?.write("TRIGGER_RELOAD\n");
		const reply = await nextStdoutLine(TRIGGER_TIMEOUT_MS, "TRIGGER_RELOAD reply");
		if (reply !== "OK TRIGGER_RELOAD") {
			throw new Error(`harness replied unexpectedly: ${reply}`);
		}
	};

	const close = async (): Promise<void> => {
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
					process.stderr.write(`[reload-harness stderr]\n${stderrLines.join("")}`);
				}
				resolve();
			}, TERM_TIMEOUT_MS);
			child.once("exit", onExit);
			// Closing stdin gives the harness a clean EOF; if it
			// doesn't shut down promptly we follow with SIGTERM.
			child.stdin?.end();
			if (!child.killed) child.kill("SIGTERM");
		});
		await exited;
	};

	return {
		clientUrl: `${ready.baseUrl}/client`,
		typeIds: ready.typeIds,
		rendererDispatcherName: ready.rendererDispatcherName,
		entityNames: ready.entityNames,
		triggerReload,
		close,
	};
}
