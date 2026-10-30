"""SQLite-backed entity store with append-only segment file storage (v6).

This module provides a high-performance entity store using:
- Append-only segment files for entity data (synchronous pread reads)
- SQLite for metadata (entity table, deps, namespaces, pins, sessions)
- Python's built-in sqlite3 module for direct SQLite access
- Connection pooling with read/write separation
"""

from fxtr2.data.sqlite_store.sqlite_namespace import (
    SQLiteFxtrSession,
    SQLiteNamespaceArena,
    SQLiteNamespaceBackend,
)
from fxtr2.data.sqlite_store.sqlite_util import ConnectionWorkerPool, ConnectionWorkerPoolGroup

__all__ = [
    "SQLiteFxtrSession",
    "SQLiteNamespaceBackend",
    "SQLiteNamespaceArena",
    "ConnectionWorkerPool",
    "ConnectionWorkerPoolGroup",
]
