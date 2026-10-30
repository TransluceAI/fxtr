/**
 * TypeScript classes for handling escaped JSON format from the debug server.
 * These correspond to the special encoded types in the escaped JSON format.
 */

export class CidString {
  constructor(public readonly base32_cid_str: string) {}

  toString(): string {
    return this.base32_cid_str;
  }
}

export class BytesObject {
  constructor(public readonly base64_byte_str: string) {}

  toString(): string {
    return this.base64_byte_str;
  }
}

/**
 * Converts escaped JSON format to TypeScript objects with proper CID and Bytes handling.
 * Similar to dag_util.escaped_atjson_to_dag_cbor but produces TypeScript objects.
 */
export function convertEscapedJson(value: any): any {
  if (value === null || value === undefined) {
    return value;
  }

  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    return value;
  }

  if (Array.isArray(value)) {
    return value.map(item => convertEscapedJson(item));
  }

  if (typeof value === 'object') {
    // Handle special escaped single-key objects (matching dag_util.py logic)
    if (Object.keys(value).length === 1) {
      const key = Object.keys(value)[0];
      const val = value[key];

      // Handle CID objects
      if (key === '$link' && typeof val === 'string') {
        return new CidString(val);
      }

      // Handle bytes objects
      if (key === '$bytes' && typeof val === 'string') {
        return new BytesObject(val);
      }

      // Handle escaped keys (remove one layer of "$\" escaping)
      if (key.startsWith('$\\')) {
        const unescapedKey = key.substring(2); // Remove "$\"
        return { [unescapedKey]: convertEscapedJson(val) };
      }
    }

    // Regular object - recursively convert all values
    const converted: any = {};
    for (const [key, val] of Object.entries(value)) {
      converted[key] = convertEscapedJson(val);
    }

    return converted;
  }

  return value;
}
