/**
 * Spawns the demo workstation Python script as a child process,
 * reads `READY <url>` from its stdout to discover the bound URL,
 * and returns a handle that tears it down on close.
 *
 * Used by `integration.test.ts`; not used by the MockWebSocket-
 * driven unit tests in the same directory.
 *
 * The script lives at `tests/_demo_workstation.py` and is run via
 * `uv run python tests/_demo_workstation.py` (uv walks up to find
 * the workstation package's pyproject.toml).
 */

import { type ChildProcess, spawn } from "node:child_process";
import path from "node:path";

export interface DemoWorkstation {
	readonly clientUrl: string;
	close(): Promise<void>;
}

const READY_TIMEOUT_MS = 15_000;
const TERM_TIMEOUT_MS = 5_000;
const DEMO_SCRIPT = path.resolve(__dirname, "_demo_workstation.py");

/**
 * Launch the demo workstation. Resolves once the child prints
 * `READY <url>`. Rejects if startup fails or times out.
 *
 * The returned `clientUrl` is the workstation's `/client`
 * endpoint, e.g. `ws://127.0.0.1:62960/client`.
 */
export async function startDemoWorkstation(): Promise<DemoWorkstation> {
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
		clientUrl: `${baseUrl}/client`,
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
			// Force-kill on any startup failure so we don't leave an
			// orphaned child.
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
			settleErr(new Error(`demo workstation exited before READY (code=${code}, signal=${signal})`));
		};
		const onError = (err: Error) => {
			settleErr(new Error(`demo workstation spawn error: ${err.message}`));
		};
		const onTimeout = () => {
			settleErr(new Error(`demo workstation did not print READY within ${READY_TIMEOUT_MS}ms`));
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
 * Send SIGTERM and wait for the child to exit. If it doesn't
 * exit within `TERM_TIMEOUT_MS`, follow up with SIGKILL.
 *
 * We track exit via the `exit` event rather than `child.killed`
 * — `child.killed` flips to `true` immediately after any signal
 * is sent (including SIGTERM), so checking it inside the
 * fallback timer would always short-circuit and never escalate
 * to SIGKILL.
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
			// SIGKILL — and surface stderr if the SIGTERM didn't
			// land, since something's wedged.
			console.warn(
				`demo workstation didn't exit on SIGTERM within ${TERM_TIMEOUT_MS}ms; sending SIGKILL. Stderr:`,
				stderrLines.join(""),
			);
			child.kill("SIGKILL");
		}, TERM_TIMEOUT_MS);
	});
}
