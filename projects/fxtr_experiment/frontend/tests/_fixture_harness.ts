/**
 * Spawns the dialect fixture backend
 * (`examples/fxtr_experiment_demo/dialect_fixture_backend.py`) as a
 * child process and reads `READY <baseUrl>` from its stdout — the
 * Python half of the live dialect round-trip test. Mirrors
 * `connector-http/tests/_integration_harness.ts`; the backend runs via
 * `uv run python` with the demo package's env (cwd = the demo dir, so
 * uv resolves its pyproject — it carries `fxtr_experiment` + the
 * `fxtr2.experimental.http` service layer). `uv run` forwards SIGTERM
 * and the backend exits gracefully on it.
 */

import { type ChildProcess, spawn } from "node:child_process";
import path from "node:path";

export interface FixtureBackend {
	readonly baseUrl: string;
	close(): Promise<void>;
}

const READY_TIMEOUT_MS = 60_000;
const TERM_TIMEOUT_MS = 5_000;
const DEMO_DIR = path.resolve(__dirname, "../../../..", "examples/fxtr_experiment_demo");

export async function startFixtureBackend(): Promise<FixtureBackend> {
	const child = spawn("uv", ["run", "python", "dialect_fixture_backend.py"], {
		stdio: ["ignore", "pipe", "pipe"],
		cwd: DEMO_DIR,
	});

	const stderrLines: string[] = [];
	child.stderr?.on("data", (chunk: Buffer) => {
		stderrLines.push(chunk.toString());
	});

	const baseUrl = await readReadyLine(child, stderrLines);
	return {
		baseUrl,
		close: () => stopChild(child, stderrLines),
	};
}

function readReadyLine(child: ChildProcess, stderrLines: string[]): Promise<string> {
	return new Promise<string>((resolve, reject) => {
		let buf = "";
		let settled = false;
		const settleErr = (err: Error) => {
			if (settled) return;
			settled = true;
			cleanup();
			if (!child.killed) child.kill("SIGKILL");
			reject(new Error(`${err.message}\nbackend stderr:\n${stderrLines.join("")}`));
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
			settleErr(new Error(`fixture backend exited before READY (code=${code}, signal=${signal})`));
		};
		const onError = (err: Error) => {
			settleErr(new Error(`fixture backend spawn error: ${err.message}`));
		};
		const onTimeout = () => {
			settleErr(new Error(`fixture backend did not print READY within ${READY_TIMEOUT_MS}ms`));
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
				`fixture backend didn't exit on SIGTERM within ${TERM_TIMEOUT_MS}ms; sending SIGKILL. Stderr:`,
				stderrLines.join(""),
			);
			child.kill("SIGKILL");
		}, TERM_TIMEOUT_MS);
	});
}
