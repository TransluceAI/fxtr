/**
 * Spawns the kitchen-sink stateless backend (`_demo_backend.py`) as a
 * child process, reads `READY <baseUrl>` from its stdout to discover the
 * bound URL, and returns a handle that tears it down on close.
 *
 * Used by `integration.test.ts`; not used by the MockWebSocket/mock-fetch
 * unit tests in the same directory. The backend (`fxtr2.experimental.http`,
 * the `service_*` library) lives in the `fxtr2` package, which is an
 * editable dependency of the workstation env — so we run the script via
 * `uv run python` (uv walks up to find the workstation package's
 * pyproject.toml and uses that venv). `uv run` forwards `SIGTERM` to its
 * child, and the backend shuts down gracefully on it.
 *
 * Mirrors `connector-workstation/tests/_integration_harness.ts` in shape;
 * the READY line here carries an http base URL (not a ws `/client`).
 */

import { type ChildProcess, spawn } from "node:child_process";
import path from "node:path";

export interface DemoBackend {
	/** The backend's HTTP base URL, e.g. `http://127.0.0.1:57084`. */
	readonly baseUrl: string;
	close(): Promise<void>;
}

const READY_TIMEOUT_MS = 30_000;
const TERM_TIMEOUT_MS = 5_000;
const DEMO_SCRIPT = path.resolve(__dirname, "_demo_backend.py");

/**
 * Launch the demo backend. Resolves once the child prints
 * `READY <baseUrl>`. Rejects if startup fails or times out.
 */
export async function startDemoBackend(): Promise<DemoBackend> {
	const child = spawn("uv", ["run", "python", DEMO_SCRIPT], {
		stdio: ["ignore", "pipe", "pipe"],
		// Run from the script's directory so `uv` finds the workstation
		// package's pyproject.toml by walking up.
		cwd: path.dirname(DEMO_SCRIPT),
	});

	const stderrLines: string[] = [];
	child.stderr?.on("data", (chunk: Buffer) => {
		stderrLines.push(chunk.toString());
	});

	const baseUrl = await readReadyLine(child);
	return {
		baseUrl,
		close: () => stopChild(child, stderrLines),
	};
}

/**
 * Read child's stdout line-by-line until we see `READY <url>`.
 * Rejects on premature exit, stream error, or timeout.
 */
function readReadyLine(child: ChildProcess): Promise<string> {
	return new Promise<string>((resolve, reject) => {
		let buf = "";
		let settled = false;
		const settleErr = (err: Error) => {
			if (settled) return;
			settled = true;
			cleanup();
			if (!child.killed) child.kill("SIGKILL");
			reject(err);
		};
		const settleOk = (url: string) => {
			if (settled) return;
			settled = true;
			cleanup();
			resolve(url);
		};

		const onData = (chunk: Buffer) => {
			buf += chunk.toString();
			let nl = buf.indexOf("\n");
			while (nl !== -1) {
				const line = buf.slice(0, nl);
				buf = buf.slice(nl + 1);
				const match = line.match(/^READY (.+)$/);
				if (match?.[1]) {
					settleOk(match[1]);
					return;
				}
				nl = buf.indexOf("\n");
			}
		};
		const onExit = (code: number | null, signal: NodeJS.Signals | null) => {
			settleErr(new Error(`demo backend exited before READY (code=${code}, signal=${signal})`));
		};
		const onError = (err: Error) => {
			settleErr(new Error(`demo backend spawn error: ${err.message}`));
		};
		const onTimeout = () => {
			settleErr(new Error(`demo backend did not print READY within ${READY_TIMEOUT_MS}ms`));
		};

		const timer = setTimeout(onTimeout, READY_TIMEOUT_MS);
		const cleanup = () => {
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

/**
 * Send SIGTERM and wait for the child to exit; escalate to SIGKILL if it
 * doesn't exit within `TERM_TIMEOUT_MS`. Tracks exit via the `exit` event
 * (not `child.killed`, which flips immediately on any signal).
 */
function stopChild(child: ChildProcess, stderrLines: string[]): Promise<void> {
	return new Promise<void>((resolve) => {
		if (child.exitCode !== null || child.signalCode !== null) {
			resolve();
			return;
		}
		let exited = false;
		const onExit = () => {
			exited = true;
			clearTimeout(killTimer);
			resolve();
		};
		child.once("exit", onExit);
		child.kill("SIGTERM");
		const killTimer = setTimeout(() => {
			if (exited) return;
			console.warn(
				`demo backend didn't exit on SIGTERM within ${TERM_TIMEOUT_MS}ms; sending SIGKILL. Stderr:`,
				stderrLines.join(""),
			);
			child.kill("SIGKILL");
		}, TERM_TIMEOUT_MS);
	});
}
