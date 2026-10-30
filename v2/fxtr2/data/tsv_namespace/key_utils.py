"""Pin key parsing and validation for tsv namespaces.

Pin keys map to TSV file locations:

| Key                    | TSV File               | Key in TSV |
|------------------------|------------------------|------------|
| `mykey`                | `index.fxtr.tsv`       | `mykey`    |
| `data/models:encoder`  | `data/models.fxtr.tsv` | `encoder`  |
| `foo/bar:baz`          | `foo/bar.fxtr.tsv`     | `baz`      |

Rules:
- Keys with no colon go into the reserved prefix TSV (default: `index.fxtr.tsv`)
- Keys with exactly one colon: part before colon is the path, part after is the key in that TSV
- Keys with more than one colon are an error
- The reserved prefix cannot be used as a path prefix to avoid ambiguity
- Keys cannot contain tabs or newlines
"""

from __future__ import annotations


class FxtrKeyError(Exception):
    """Error related to pin key format or validation."""


def validate_pin_key(full_key: str, reserved_prefix: str = "index") -> None:
    """Validate a pin key, raising FxtrKeyError if invalid.

    Args:
        full_key: The full pin key to validate.
        reserved_prefix: The reserved prefix for keys without colons.

    Raises:
        FxtrKeyError: If the key is invalid.
    """
    if not full_key:
        raise FxtrKeyError("Pin key cannot be empty")

    if "\t" in full_key:
        raise FxtrKeyError(f"Pin key cannot contain tabs: {full_key!r}")
    if "\n" in full_key:
        raise FxtrKeyError(f"Pin key cannot contain newlines: {full_key!r}")
    if "\r" in full_key:
        raise FxtrKeyError(f"Pin key cannot contain carriage returns: {full_key!r}")

    colon_count = full_key.count(":")
    if colon_count > 1:
        raise FxtrKeyError(f"Pin key cannot contain more than one colon: {full_key!r}")

    if colon_count == 1:
        path_prefix, tsv_key = full_key.split(":", 1)

        # Validate path prefix
        if not path_prefix:
            raise FxtrKeyError(f"Path prefix cannot be empty in key: {full_key!r}")

        # Check reserved prefix conflict
        if reserved_prefix and path_prefix == reserved_prefix:
            raise FxtrKeyError(
                f"The prefix {reserved_prefix!r} is reserved. "
                f"Use a different path prefix, or change the reserved_prefix setting "
                f"in fxtr_namespace.toml."
            )

        # Validate tsv key (no colons allowed in TSV keys)
        if not tsv_key:
            raise FxtrKeyError(f"Key after colon cannot be empty in key: {full_key!r}")

    # For keys without colon, they become the TSV key directly
    # No additional validation needed beyond the character checks above


def parse_pin_key(full_key: str, reserved_prefix: str = "index") -> tuple[str, str]:
    """Parse a full pin key into (rel_tsv_path, tsv_key).

    Examples:
        "mykey" -> ("index.fxtr.tsv", "mykey")  # using default reserved_prefix="index"
        "data/models:encoder" -> ("data/models.fxtr.tsv", "encoder")
        "foo/bar:baz" -> ("foo/bar.fxtr.tsv", "baz")

    Args:
        full_key: The full pin key to parse.
        reserved_prefix: The reserved prefix for keys without colons.
            If empty string, keys without colons are not allowed.

    Returns:
        Tuple of (rel_tsv_path, tsv_key) where rel_tsv_path is relative to the store root.

    Raises:
        FxtrKeyError: If the key is invalid.
    """
    # Validate first
    validate_pin_key(full_key, reserved_prefix)

    if ":" in full_key:
        # Key with path prefix
        path_prefix, tsv_key = full_key.split(":", 1)
        return f"{path_prefix}.fxtr.tsv", tsv_key
    else:
        # Key without colon - use reserved prefix
        if not reserved_prefix:
            raise FxtrKeyError(
                f"Keys without colon are not allowed when reserved_prefix is empty: {full_key!r}"
            )
        return f"{reserved_prefix}.fxtr.tsv", full_key


def make_full_key(rel_tsv_path: str, tsv_key: str, reserved_prefix: str = "index") -> str:
    """Construct a full pin key from TSV path and TSV key.

    This is the inverse of parse_pin_key.

    Args:
        rel_tsv_path: Relative path to the TSV file (e.g. "index.fxtr.tsv" or "data/models.fxtr.tsv").
        tsv_key: The key within the TSV file.
        reserved_prefix: The reserved prefix for the index TSV.

    Returns:
        The full pin key.

    Raises:
        FxtrKeyError: If the TSV path doesn't have the expected suffix.
    """
    # Validate tsv_key doesn't have colons (it shouldn't be in a TSV if it does)
    if ":" in tsv_key:
        raise FxtrKeyError(f"TSV key cannot contain colons: {tsv_key!r}")

    # Extract the filename from the path
    expected_suffix = ".fxtr.tsv"
    slash_pos = rel_tsv_path.rfind("/")
    if slash_pos == -1:
        tsv_name = rel_tsv_path
        parent = ""
    else:
        tsv_name = rel_tsv_path[slash_pos + 1 :]
        parent = rel_tsv_path[:slash_pos]

    if not tsv_name.endswith(expected_suffix):
        raise FxtrKeyError(f"TSV path must end with {expected_suffix!r}: {rel_tsv_path}")

    # Get the stem without .fxtr.tsv
    stem = tsv_name[: -len(expected_suffix)]

    # Reconstruct the path prefix
    if parent:
        path_prefix = f"{parent}/{stem}"
    else:
        path_prefix = stem

    # Check if this is the reserved prefix TSV
    if path_prefix == reserved_prefix:
        return tsv_key
    else:
        return f"{path_prefix}:{tsv_key}"
