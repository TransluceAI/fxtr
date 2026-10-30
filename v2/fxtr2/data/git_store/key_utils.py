"""Pin key parsing and validation for git-backed fxtr stores.

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

from pathlib import Path


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
                f"in fxtr_store.toml."
            )

        # Validate tsv key (no colons allowed in TSV keys)
        if not tsv_key:
            raise FxtrKeyError(f"Key after colon cannot be empty in key: {full_key!r}")

    # For keys without colon, they become the TSV key directly
    # No additional validation needed beyond the character checks above


def parse_pin_key(full_key: str, reserved_prefix: str = "index") -> tuple[Path, str]:
    """Parse a full pin key into (tsv_path, tsv_key).

    Examples:
        "mykey" -> (Path("index.fxtr.tsv"), "mykey")  # using default reserved_prefix="index"
        "data/models:encoder" -> (Path("data/models.fxtr.tsv"), "encoder")
        "foo/bar:baz" -> (Path("foo/bar.fxtr.tsv"), "baz")

    Args:
        full_key: The full pin key to parse.
        reserved_prefix: The reserved prefix for keys without colons.
            If empty string, keys without colons are not allowed.

    Returns:
        Tuple of (tsv_path, tsv_key) where tsv_path is relative to the store root.

    Raises:
        FxtrKeyError: If the key is invalid.
    """
    # Validate first
    validate_pin_key(full_key, reserved_prefix)

    if ":" in full_key:
        # Key with path prefix
        path_prefix, tsv_key = full_key.split(":", 1)
        tsv_path = Path(f"{path_prefix}.fxtr.tsv")
        return tsv_path, tsv_key
    else:
        # Key without colon - use reserved prefix
        if not reserved_prefix:
            raise FxtrKeyError(
                f"Keys without colon are not allowed when reserved_prefix is empty: {full_key!r}"
            )
        tsv_path = Path(f"{reserved_prefix}.fxtr.tsv")
        return tsv_path, full_key


def make_full_key(tsv_path: Path, tsv_key: str, reserved_prefix: str = "index") -> str:
    """Construct a full pin key from TSV path and TSV key.

    This is the inverse of parse_pin_key.

    Args:
        tsv_path: Path to the TSV file (relative to store root).
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

    # Extract the path prefix from the TSV path
    tsv_name = tsv_path.name
    expected_suffix = ".fxtr.tsv"
    if not tsv_name.endswith(expected_suffix):
        raise FxtrKeyError(f"TSV path must end with {expected_suffix!r}: {tsv_path}")

    # Get the stem without .fxtr.tsv
    stem = tsv_name[: -len(expected_suffix)]

    # Reconstruct the path prefix
    if tsv_path.parent == Path("."):
        path_prefix = stem
    else:
        path_prefix = str(tsv_path.parent / stem)

    # Check if this is the reserved prefix TSV
    if path_prefix == reserved_prefix:
        return tsv_key
    else:
        return f"{path_prefix}:{tsv_key}"
