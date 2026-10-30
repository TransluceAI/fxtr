/**
 * Core entity types for the fxtr system.
 *
 * Ported from Python's entity_core.py. Contains:
 * - FxtrID: Immutable entity identifier
 * - Tagged ID wrappers: RawWeakFxtrID, RawStrongFxtrID, RawFxtrEntityMarkerID
 * - FxtrCbor / FrozenFxtrCbor type definitions
 * - CBOR encode/decode with fxtr tags
 * - Base32 and varint32 helpers
 */

import { decode, encode } from "cbor2";
import { sortLengthFirstDeterministic } from "cbor2/sorts";

// ---------------------------------------------------------------------------
// Uint8Array utilities
// ---------------------------------------------------------------------------

/** Compare two Uint8Arrays for value equality. */
export function uint8ArrayEquals(a: Uint8Array, b: Uint8Array): boolean {
	if (a.length !== b.length) return false;
	for (let i = 0; i < a.length; i++) {
		if (a[i] !== b[i]) return false;
	}
	return true;
}

// ---------------------------------------------------------------------------
// Base32 encoding/decoding (lowercase, no padding)
// ---------------------------------------------------------------------------

const B32_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567";

/** Encode bytes to lowercase base32 without padding. */
export function b32enc(data: Uint8Array): string {
	let bits = 0;
	let buffer = 0;
	let result = "";
	for (const byte of data) {
		buffer = (buffer << 8) | byte;
		bits += 8;
		while (bits >= 5) {
			bits -= 5;
			// biome-ignore lint/style/noNonNullAssertion: & 0x1f masks to 0-31, alphabet is 32 chars
			result += B32_ALPHABET[(buffer >> bits) & 0x1f]!;
		}
	}
	if (bits > 0) {
		// biome-ignore lint/style/noNonNullAssertion: & 0x1f masks to 0-31, alphabet is 32 chars
		result += B32_ALPHABET[(buffer << (5 - bits)) & 0x1f]!;
	}
	return result;
}

/** Decode lowercase base32 (no padding) to bytes. */
export function b32dec(data: string): Uint8Array {
	let bits = 0;
	let buffer = 0;
	const result: number[] = [];
	for (const char of data.toLowerCase()) {
		const idx = B32_ALPHABET.indexOf(char);
		if (idx === -1) {
			throw new Error(`Invalid base32 character: ${char}`);
		}
		buffer = (buffer << 5) | idx;
		bits += 5;
		if (bits >= 8) {
			bits -= 8;
			result.push((buffer >> bits) & 0xff);
		}
	}
	return new Uint8Array(result);
}

// ---------------------------------------------------------------------------
// Varint32 encoding/decoding (custom base-32 varint for p-node paths)
// ---------------------------------------------------------------------------

const VARINT32_ALPHABET = "0123456789abcdefghijklmnopqrstuv";

/** Encode a non-negative integer as a varint32 string. */
export function encodeVarint32(value: number): string {
	if (value < 0) {
		throw new Error("Only non-negative integers supported");
	}
	if (value === 0) {
		// biome-ignore lint/style/noNonNullAssertion: constant index 0, alphabet is 32 chars
		return VARINT32_ALPHABET[0]!;
	}

	// Collect nibbles from low to high
	const nibbles: number[] = [];
	let v = value;
	while (v > 0) {
		nibbles.push(v & 0xf);
		v >>= 4;
	}

	// Reverse to get high-to-low order (big-endian)
	nibbles.reverse();

	// Encode: all but last are continuation (offset by 16), last is terminal
	let result = "";
	for (let i = 0; i < nibbles.length; i++) {
		if (i < nibbles.length - 1) {
			// biome-ignore lint/style/noNonNullAssertion: i bounded by loop; nibble+16 max 31, alphabet is 32 chars
			result += VARINT32_ALPHABET[nibbles[i]! + 16]!; // Continuation
		} else {
			// biome-ignore lint/style/noNonNullAssertion: i bounded by loop; nibble max 15, alphabet is 32 chars
			result += VARINT32_ALPHABET[nibbles[i]!]!; // Terminal
		}
	}
	return result;
}

/** Decode a varint32 string, returning [value, remaining_string]. */
export function decodeVarint32(s: string): [number, string] {
	let value = 0;
	for (let i = 0; i < s.length; i++) {
		// biome-ignore lint/style/noNonNullAssertion: i is within s.length bounds
		const idx = VARINT32_ALPHABET.indexOf(s[i]!);
		if (idx === -1) {
			throw new Error(`Invalid varint32 character: ${s[i]}`);
		}
		const data = idx & 0xf; // Extract low 4 bits
		value = (value << 4) | data;
		if (idx < 16) {
			// Terminal character
			return [value, s.slice(i + 1)];
		}
	}
	throw new Error(`Invalid varint32 string: ${s}`);
}

// ---------------------------------------------------------------------------
// FxtrID
// ---------------------------------------------------------------------------

/**
 * An immutable identifier for an entity in the fxtr graph.
 *
 * Each FxtrID holds raw ID bytes: a single-character tag byte followed by
 * a tag-specific payload. The tag determines the node type (d, n, c, p, !, ., u).
 *
 * Use `id.key()` (which returns `idString()`) as the canonical string key
 * for Map<string, ...> and Set<string>.
 */
export class FxtrID {
	readonly rawDataId: Uint8Array;
	private _idString: string | null = null;

	constructor(rawDataId: Uint8Array) {
		this.rawDataId = rawDataId;
	}

	/** The tag character (first byte). */
	getTag(): string {
		// biome-ignore lint/style/noNonNullAssertion: rawDataId always has at least 1 byte (tag)
		return String.fromCharCode(this.rawDataId[0]!);
	}

	/** Split into [tag, payload]. */
	splitTagAndPayload(): [string, Uint8Array] {
		// biome-ignore lint/style/noNonNullAssertion: rawDataId always has at least 1 byte (tag)
		return [String.fromCharCode(this.rawDataId[0]!), this.rawDataId.slice(1)];
	}

	/** Canonical string representation. */
	idString(): string {
		if (this._idString !== null) {
			return this._idString;
		}
		const result = this._computeIdString();
		this._idString = result;
		return result;
	}

	private _computeIdString(): string {
		const [tag, payload] = this.splitTagAndPayload();
		switch (tag) {
			case ".": {
				if (payload.length !== 0) {
					throw new Error("Expected empty payload for '.' node");
				}
				return ".";
			}
			case "d":
				return `d${b32enc(payload)}`;
			case "c":
				return `c${b32enc(payload)}`;
			case "n":
				return `n${b32enc(payload)}`;
			case "p": {
				// Payload is CBOR-encoded [hash_bytes, ...path_indices]
				const wrapped = decode(payload) as [Uint8Array, ...number[]];
				if (!Array.isArray(wrapped) || wrapped.length < 1) {
					throw new Error("Invalid p-node payload");
				}
				const fst = b32enc(wrapped[0] as Uint8Array);
				const tail = wrapped
					.slice(1)
					.map((n) => encodeVarint32(n as number))
					.join("");
				return `p${fst}[${tail}]`;
			}
			case "!": {
				const builtinName = new TextDecoder().decode(payload);
				return `!${builtinName}`;
			}
			case "u": {
				const innerNode = new FxtrID(payload);
				return `u${innerNode.idString()}`;
			}
			default:
				throw new Error(`Unknown tag: ${tag}`);
		}
	}

	/** Short summary string for debugging. */
	summary(): string {
		const [tag, payload] = this.splitTagAndPayload();
		switch (tag) {
			case ".":
				return ".";
			case "d":
				return `d${b32enc(payload).slice(0, 7)}`;
			case "c":
				return `c${b32enc(payload).slice(0, 7)}`;
			case "n":
				return `n${b32enc(payload).slice(0, 7)}`;
			case "p": {
				const wrapped = decode(payload) as [Uint8Array, ...number[]];
				const fst = b32enc(wrapped[0] as Uint8Array);
				const tail = wrapped
					.slice(1)
					.map((n) => encodeVarint32(n as number))
					.join("");
				return `p${fst.slice(0, 4)}[${tail}]`;
			}
			case "!": {
				const builtinName = new TextDecoder().decode(payload);
				return `!${builtinName}`;
			}
			case "u": {
				const innerNode = new FxtrID(payload);
				return `u${innerNode.summary()}`;
			}
			default:
				throw new Error(`Unknown tag: ${tag}`);
		}
	}

	/** String key for use in Map/Set. Same as idString(). */
	key(): string {
		return this.idString();
	}

	toString(): string {
		return this.idString();
	}

	/** Value equality: compare raw ID bytes. */
	equals(other: FxtrID): boolean {
		return uint8ArrayEquals(this.rawDataId, other.rawDataId);
	}

	// --- Constructors ---

	static fromTagAndPayload(tag: string, payload: Uint8Array): FxtrID {
		const raw = new Uint8Array(1 + payload.length);
		raw[0] = tag.charCodeAt(0);
		raw.set(payload, 1);
		return new FxtrID(raw);
	}

	static fromIdString(idStr: string): FxtrID {
		if (!idStr) {
			throw new Error("Empty ID string");
		}

		// biome-ignore lint/style/noNonNullAssertion: checked non-empty above
		const tag = idStr[0]!;
		const rest = idStr.slice(1);

		switch (tag) {
			case ".": {
				if (rest) {
					throw new Error(`Expected empty string after '.', got: ${rest}`);
				}
				return FxtrID.fromTagAndPayload(".", new Uint8Array(0));
			}
			case "d":
			case "c":
			case "n":
				return FxtrID.fromTagAndPayload(tag, b32dec(rest));
			case "p": {
				const bracketIdx = rest.indexOf("[");
				if (bracketIdx === -1) {
					throw new Error("Invalid p-ID format: missing '['");
				}
				const fstStr = rest.slice(0, bracketIdx);
				let tailStr = rest.slice(bracketIdx + 1);
				if (!tailStr.endsWith("]")) {
					throw new Error("Invalid p-ID format: missing ']'");
				}
				tailStr = tailStr.slice(0, -1); // Remove trailing ']'

				const fstBytes = b32dec(fstStr);
				const path: number[] = [];
				let remaining = tailStr;
				while (remaining) {
					const [value, rest] = decodeVarint32(remaining);
					path.push(value);
					remaining = rest;
				}

				const wrapped: [Uint8Array, ...number[]] = [fstBytes, ...path];
				const payload = encode(wrapped, {
					sortKeys: sortLengthFirstDeterministic,
				});
				return FxtrID.fromTagAndPayload("p", payload);
			}
			case "!": {
				const payload = new TextEncoder().encode(rest);
				return FxtrID.fromTagAndPayload("!", payload);
			}
			case "u": {
				const innerId = FxtrID.fromIdString(rest);
				return FxtrID.fromTagAndPayload("u", innerId.rawDataId);
			}
			default:
				throw new Error(`Unknown tag: ${tag}`);
		}
	}

	/** Create a builtin ID (!name). */
	static builtin(name: string): FxtrID {
		return FxtrID.fromTagAndPayload("!", new TextEncoder().encode(name));
	}

	/** Create the "here" ground node (.). */
	static here(): FxtrID {
		return FxtrID.fromTagAndPayload(".", new Uint8Array(0));
	}
}

// ---------------------------------------------------------------------------
// Tagged ID wrappers (CBOR tags 33000, 33001, 33002)
// ---------------------------------------------------------------------------

/** Weak reference to another entity (CBOR tag 33001). */
export class RawWeakFxtrID {
	static readonly CBOR_TAG = 33001;
	readonly innerId: FxtrID;

	constructor(innerId: FxtrID) {
		this.innerId = innerId;
	}

	toCBOR(): [number, Uint8Array] {
		return [33001, this.innerId.rawDataId];
	}
}

/** Strong (child) reference to another entity (CBOR tag 33002). */
export class RawStrongFxtrID {
	static readonly CBOR_TAG = 33002;
	readonly innerId: FxtrID;

	constructor(innerId: FxtrID) {
		this.innerId = innerId;
	}

	toCBOR(): [number, Uint8Array] {
		return [33002, this.innerId.rawDataId];
	}
}

/** Entity marker / context ID (CBOR tag 33000). Appears exactly once per entity at $fxtr.ctx. */
export class RawFxtrEntityMarkerID {
	static readonly CBOR_TAG = 33000;
	readonly innerId: FxtrID;

	constructor(innerId: FxtrID) {
		this.innerId = innerId;
	}

	toCBOR(): [number, Uint8Array] {
		return [33000, this.innerId.rawDataId];
	}
}

/** Union of all tagged ID types. */
export type RawFxtrRef = RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID;

// ---------------------------------------------------------------------------
// FxtrCbor types
// ---------------------------------------------------------------------------

/**
 * CBOR value with fxtr tags. Readonly at the type level — consumers must not
 * mutate values of this type. Mutable locals (e.g. `Record<string, FxtrCbor>`)
 * are assignable to FxtrCbor, so construction still works naturally.
 *
 * Runtime immutability can be enforced via `deepFreeze()` where needed.
 */
export type FxtrCbor =
	| null
	| number
	| boolean
	| string
	| Uint8Array
	| readonly FxtrCbor[]
	| { readonly [key: string]: FxtrCbor }
	| RawWeakFxtrID
	| RawStrongFxtrID
	| RawFxtrEntityMarkerID;

// ---------------------------------------------------------------------------
// CBOR encode/decode with fxtr tags
// ---------------------------------------------------------------------------

/** Fxtr tag decoders for cbor2. Tag decoder receives { tag, contents }. */
const fxtrTagDecoders = new Map([
	[
		33000,
		(tag: { contents: unknown }) =>
			new RawFxtrEntityMarkerID(new FxtrID(tag.contents as Uint8Array)),
	],
	[
		33001,
		(tag: { contents: unknown }) => new RawWeakFxtrID(new FxtrID(tag.contents as Uint8Array)),
	],
	[
		33002,
		(tag: { contents: unknown }) => new RawStrongFxtrID(new FxtrID(tag.contents as Uint8Array)),
	],
]);

/** Encode options for RFC 7049 canonical encoding. */
const fxtrEncodeOptions = { sortKeys: sortLengthFirstDeterministic };

/** Decode options with fxtr tag decoders. */
const fxtrDecodeOptions = { tags: fxtrTagDecoders };

/** Encode a CBOR value with fxtr tags. Uses RFC 7049 canonical key ordering. */
export function fxtrCborDumps(value: FxtrCbor): Uint8Array {
	return encode(value, fxtrEncodeOptions);
}

/** Decode CBOR bytes with fxtr tag support. */
export function fxtrCborLoads(data: Uint8Array): FxtrCbor {
	return decode(data, fxtrDecodeOptions) as FxtrCbor;
}

// ---------------------------------------------------------------------------
// Entity marker validation
// ---------------------------------------------------------------------------

/** Error raised when an entity structure is invalid. */
export class InvalidEntityError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "InvalidEntityError";
	}
}

/** Error raised when an entity has more than one RawFxtrEntityMarkerID. */
export class DuplicateEntityMarkerError extends InvalidEntityError {
	constructor(message: string) {
		super(message);
		this.name = "DuplicateEntityMarkerError";
	}
}

function isPlainObject(value: unknown): value is Record<string, FxtrCbor> {
	return (
		typeof value === "object" &&
		value !== null &&
		!Array.isArray(value) &&
		!(value instanceof Uint8Array) &&
		!(value instanceof RawWeakFxtrID) &&
		!(value instanceof RawStrongFxtrID) &&
		!(value instanceof RawFxtrEntityMarkerID)
	);
}

/** Count RawFxtrEntityMarkerID instances in a CBOR tree. */
function countMarkers(root: FxtrCbor): number {
	if (root instanceof RawFxtrEntityMarkerID) {
		return 1;
	}
	if (root instanceof RawWeakFxtrID || root instanceof RawStrongFxtrID) {
		return 0;
	}
	if (Array.isArray(root)) {
		let count = 0;
		for (const item of root) {
			count += countMarkers(item);
		}
		return count;
	}
	if (isPlainObject(root)) {
		let count = 0;
		for (const value of Object.values(root)) {
			count += countMarkers(value);
		}
		return count;
	}
	return 0;
}

/**
 * Encode an entity with validation that it has exactly one RawFxtrEntityMarkerID at $fxtr.ctx.
 */
export function fxtrCborDumpsWithMarkerCheck(value: FxtrCbor): Uint8Array {
	if (!isPlainObject(value)) {
		throw new InvalidEntityError("Entity must be a plain object");
	}
	if (!("$fxtr.ctx" in value)) {
		throw new InvalidEntityError('Entity must have a "$fxtr.ctx" field');
	}
	if (!(value["$fxtr.ctx"] instanceof RawFxtrEntityMarkerID)) {
		throw new InvalidEntityError('"$fxtr.ctx" field must be a RawFxtrEntityMarkerID');
	}
	const markerCount = countMarkers(value);
	if (markerCount !== 1) {
		throw new DuplicateEntityMarkerError(
			"Found more than one RawFxtrEntityMarkerID in this entity",
		);
	}
	return fxtrCborDumps(value);
}

// ---------------------------------------------------------------------------
// Deep freeze utility
// ---------------------------------------------------------------------------

/**
 * Recursively freeze a CBOR value for runtime immutability enforcement.
 *
 * FxtrCbor is readonly at the type level, but `deepFreeze` adds runtime
 * enforcement via `Object.freeze` — useful for defense-in-depth and
 * maintaining caching invariants.
 */
export function deepFreeze(obj: FxtrCbor): FxtrCbor {
	if (obj === null || typeof obj !== "object") {
		return obj;
	}
	if (
		obj instanceof Uint8Array ||
		obj instanceof RawWeakFxtrID ||
		obj instanceof RawStrongFxtrID ||
		obj instanceof RawFxtrEntityMarkerID
	) {
		return Object.freeze(obj);
	}
	if (Array.isArray(obj)) {
		for (const item of obj) {
			deepFreeze(item);
		}
		return Object.freeze(obj) as unknown as FxtrCbor;
	}
	// Plain object
	for (const value of Object.values(obj as Record<string, FxtrCbor>)) {
		deepFreeze(value);
	}
	return Object.freeze(obj) as FxtrCbor;
}
