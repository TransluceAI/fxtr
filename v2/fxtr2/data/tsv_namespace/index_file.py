"""Index registration file I/O for tsv namespaces.

The index registration file (fxtr_namespace_indexes.tsv) tracks which indexers are
registered at which key prefixes. It is a git-tracked file at the namespace root.

Format: one line per registration, tab-separated:
    {indexer_fxtr_id}\t{json_encoded_key_prefix}

Sorted lexicographically by full line. JSON encoding avoids ambiguity with empty
prefixes (which become "").
"""

from __future__ import annotations

import json
import os
import tempfile

from fxtr2.data.entity_core import FxtrID

from .config import TsvNamespaceConfig

INDEX_FILE_NAME = "fxtr_namespace_indexes.tsv"


def index_file_path_for_config(config: TsvNamespaceConfig) -> str:
    """Get the absolute path to the index registration file for a store.

    Args:
        config: Store configuration.

    Returns:
        Absolute path as a string.
    """
    return f"{config.namespace_root_str}/{INDEX_FILE_NAME}"


def read_index_file(path: str) -> frozenset[tuple[FxtrID, str]]:
    """Read the index registration file.

    Args:
        path: Absolute path to the index file.

    Returns:
        Frozenset of (indexer_fxtr_id, key_prefix) pairs.
        Empty frozenset if the file doesn't exist.

    Raises:
        ValueError: If the file has invalid format.
    """
    try:
        with open(path) as f:
            content = f.read()
    except FileNotFoundError:
        return frozenset()

    if not content.strip():
        return frozenset()

    registrations: set[tuple[FxtrID, str]] = set()
    for line_num, line in enumerate(content.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            raise ValueError(
                f"Invalid index file format at line {line_num}: expected "
                f"'id\\tprefix', got {line!r}"
            )
        id_str, prefix_json = parts
        try:
            fxtr_id = FxtrID.from_id_string(id_str)
        except Exception as e:
            raise ValueError(f"Invalid FxtrID at line {line_num}: {id_str!r}") from e
        try:
            prefix = json.loads(prefix_json)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON prefix at line {line_num}: {prefix_json!r}") from e
        if not isinstance(prefix, str):
            raise ValueError(
                f"Prefix must be a JSON string at line {line_num}, got {type(prefix).__name__}"
            )
        registrations.add((fxtr_id, prefix))

    return frozenset(registrations)


def write_index_file(path: str, registrations: frozenset[tuple[FxtrID, str]]) -> None:
    """Write the index registration file atomically.

    If registrations is empty, deletes the file (if it exists).
    Otherwise, writes sorted entries with atomic temp-file-and-rename.

    Args:
        path: Absolute path to the index file.
        registrations: Set of (indexer_fxtr_id, key_prefix) pairs to write.
    """
    if not registrations:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return

    # Sort by (id_string, prefix) for deterministic output
    sorted_entries = sorted(
        registrations,
        key=lambda entry: (entry[0].id_string(), entry[1]),
    )

    lines = []
    for fxtr_id, prefix in sorted_entries:
        lines.append(f"{fxtr_id.id_string()}\t{json.dumps(prefix)}\n")

    content = "".join(lines)

    # Atomic write: write to temp file in same directory, then rename
    dir_path = os.path.dirname(path)
    fd, temp_path = tempfile.mkstemp(dir=dir_path, prefix=".fxtr_indexes_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.rename(temp_path, path)
    except BaseException:
        # Clean up temp file on any error
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
