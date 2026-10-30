/**
 * MDX-to-FxtrCbor compiler subprocess.
 *
 * Reads one JSON line from stdin:
 *
 *     {
 *       "source": string,                            // MDX source
 *       "inputs": { [name]: base64 cbor },           // ref-typed inputs
 *       "passthroughComponents": string[]            // JSX names to emit as nodes
 *     }
 *
 * Writes exactly one JSON line to stdout:
 *
 *     { "ok": true, "treeCbor": base64 cbor }
 *
 * or, on a typed failure (MDX compile error, invalid input):
 *
 *     { "ok": false, "error": string }
 *
 * The encoded tree is a flat array of document-level children
 * — `evaluateMdx` strips the top-level `__fragment` wrapper
 * that MDX always emits, so consumers receive a uniform
 * `readonly unknown[]` shape.
 *
 * `passthroughComponents` lists JSX element types that should
 * flatten to `[name, props, children]` tuples (so the frontend
 * tree renderer can dispatch on them). Decoded inputs are
 * exposed to MDX as `props.fxtr_inputs.<name>`.
 */

import { decodeCbor, encodeCbor } from "./cbor_passthrough.js";
import { evaluateMdx } from "./evaluate.js";
import { passthroughCustomComponent } from "./jsx_runtime.js";

interface RequestEnvelope {
	source: string;
	inputs: Record<string, string>;
	passthroughComponents: string[];
}

interface SuccessResponse {
	ok: true;
	treeCbor: string;
}

interface ErrorResponse {
	ok: false;
	error: string;
}

type Response = SuccessResponse | ErrorResponse;

async function readStdin(): Promise<string> {
	const chunks: Buffer[] = [];
	for await (const chunk of process.stdin) {
		chunks.push(chunk as Buffer);
	}
	return Buffer.concat(chunks).toString("utf-8");
}

function decodeBase64(s: string): Uint8Array {
	return Buffer.from(s, "base64");
}

function encodeBase64(b: Uint8Array): string {
	return Buffer.from(b).toString("base64");
}

function writeResponse(resp: Response): void {
	process.stdout.write(`${JSON.stringify(resp)}\n`);
}

async function run(): Promise<void> {
	let envelope: RequestEnvelope;
	try {
		const raw = await readStdin();
		envelope = JSON.parse(raw) as RequestEnvelope;
	} catch (err) {
		writeResponse({ ok: false, error: `failed to parse request envelope: ${String(err)}` });
		return;
	}

	try {
		// Decode each input through tag-passthrough CBOR. Ref-typed
		// leaves arrive as `Tag` instances and survive verbatim.
		const decodedInputs: Record<string, unknown> = {};
		for (const [name, base64] of Object.entries(envelope.inputs)) {
			decodedInputs[name] = decodeCbor(decodeBase64(base64));
		}

		const components: Record<string, ReturnType<typeof passthroughCustomComponent>> = {};
		for (const name of envelope.passthroughComponents) {
			components[name] = passthroughCustomComponent(name);
		}

		const tree = await evaluateMdx(envelope.source, {
			components,
			props: { fxtr_inputs: decodedInputs },
		});

		const treeBytes = encodeCbor(tree);
		writeResponse({ ok: true, treeCbor: encodeBase64(treeBytes) });
	} catch (err) {
		writeResponse({ ok: false, error: String(err) });
	}
}

void run();
