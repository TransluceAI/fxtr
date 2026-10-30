"""TSV file reading and writing for git-backed fxtr stores.

TSV files are simple two-column files mapping keys to FxtrIDs. They are the
source of truth for pin mappings in clean (non-dirty) state.

Format:
- Two columns separated by tab
- First column: key (string, no tabs/newlines/colons)
- Second column: FxtrID string (e.g., "dw5d7n...")
- Keys must be sorted lexicographically
- No header row
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

from fxtr2.data.entity_core import FxtrID


class FxtrTSVError(Exception):
    """Error related to TSV file format or content."""


def validate_tsv_key(key: str) -> None:
    """Validate a key for use in a TSV file.

    TSV keys must not contain tabs, newlines, or colons.

    Args:
        key: The key to validate.

    Raises:
        FxtrTSVError: If the key contains invalid characters.
    """
    if "\t" in key:
        raise FxtrTSVError(f"TSV key cannot contain tabs: {key!r}")
    if "\n" in key:
        raise FxtrTSVError(f"TSV key cannot contain newlines: {key!r}")
    if "\r" in key:
        raise FxtrTSVError(f"TSV key cannot contain carriage returns: {key!r}")
    if ":" in key:
        raise FxtrTSVError(f"TSV key cannot contain colons: {key!r}")
    if not key:
        raise FxtrTSVError("TSV key cannot be empty")


def read_tsv(path: Path) -> dict[str, FxtrID]:
    """Read a TSV file, returning dict of tsv_key -> FxtrID.

    Validates that keys are sorted lexicographically. If the file is not sorted,
    raises FxtrTSVError with details about which keys are out of order.

    Args:
        path: Path to the TSV file.

    Returns:
        Dictionary mapping keys to FxtrIDs. Returns empty dict if file doesn't exist.

    Raises:
        FxtrTSVError: If the file is not sorted or contains invalid format.
    """
    if not path.exists():
        return {}

    entries: dict[str, FxtrID] = {}
    unsorted_keys: list[tuple[int, str, str]] = []  # (line_num, prev_key, current_key)
    prev_key: str | None = None

    with open(path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, start=1):
            # Handle line endings
            line = line.rstrip("\n\r")

            # Skip empty lines
            if not line:
                continue

            # Split on tab
            parts = line.split("\t")
            if len(parts) != 2:
                raise FxtrTSVError(
                    f"Invalid TSV format at {path}:{line_num}: "
                    f"expected 2 columns, got {len(parts)}"
                )

            key, fxtr_id_str = parts

            # Validate key format
            try:
                validate_tsv_key(key)
            except FxtrTSVError as e:
                raise FxtrTSVError(f"Invalid key at {path}:{line_num}: {e}") from e

            # Check for duplicates
            if key in entries:
                raise FxtrTSVError(f"Duplicate key at {path}:{line_num}: {key!r}")

            # Check sort order
            if prev_key is not None and key < prev_key:
                unsorted_keys.append((line_num, prev_key, key))

            # Parse FxtrID
            try:
                fxtr_id = FxtrID.from_id_string(fxtr_id_str)
            except ValueError as e:
                raise FxtrTSVError(
                    f"Invalid FxtrID at {path}:{line_num}: {fxtr_id_str!r} - {e}"
                ) from e

            entries[key] = fxtr_id
            prev_key = key

    # Report all unsorted keys
    if unsorted_keys:
        details = "\n".join(
            f"  Line {line_num}: {current!r} should come before {prev!r}"
            for line_num, prev, current in unsorted_keys
        )
        raise FxtrTSVError(
            f"TSV file is not sorted: {path}\n\n"
            f"Keys appear out of order. This usually indicates a bad git merge.\n"
            f"Please manually sort the file and commit the fix.\n\n"
            f"Out-of-order keys:\n{details}"
        )

    return entries


def write_tsv_content(path: Path, entries: dict[str, FxtrID]) -> None:
    """Write TSV content directly to a path (no atomic rename).

    Use this when the caller handles atomicity (e.g., dirty manager).
    If entries is empty, does NOT write anything (caller should handle deletion).

    Args:
        path: Path to write to.
        entries: Dictionary mapping keys to FxtrIDs.

    Raises:
        FxtrTSVError: If any key is invalid.
    """
    # Validate all keys first
    for key in entries:
        validate_tsv_key(key)

    # If empty, don't write anything
    if not entries:
        return

    # Create parent directories if needed
    path.parent.mkdir(parents=True, exist_ok=True)

    # Sort entries by key and write
    sorted_entries = sorted(entries.items(), key=lambda x: x[0])
    with path.open("w", encoding="utf-8") as f:
        for key, fxtr_id in sorted_entries:
            f.write(f"{key}\t{fxtr_id.id_string()}\n")


def write_tsv(path: Path, entries: dict[str, FxtrID]) -> None:
    """Write entries to a TSV file atomically.

    Entries are sorted by key before writing. Uses atomic write via temp file
    and rename.

    If entries is empty, deletes the file if it exists.

    Args:
        path: Path to write the TSV file.
        entries: Dictionary mapping keys to FxtrIDs.

    Raises:
        FxtrTSVError: If any key is invalid.
    """
    # Validate all keys first
    for key in entries:
        validate_tsv_key(key)

    # If empty, delete the file
    if not entries:
        if path.exists():
            path.unlink()
        return

    # Create parent directories if needed
    path.parent.mkdir(parents=True, exist_ok=True)

    # Sort entries by key
    sorted_entries = sorted(entries.items(), key=lambda x: x[0])

    # Write to temp file in same directory (for atomic rename)
    temp_fd, temp_path = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=f".tmp.{uuid.uuid4().hex[:8]}"
    )
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
            for key, fxtr_id in sorted_entries:
                f.write(f"{key}\t{fxtr_id.id_string()}\n")

        # Atomic rename
        os.rename(temp_path, path)
    except Exception:
        # Clean up temp file on error
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
