"""SQLite-backed entity store using sqlite3.

This module provides a high-performance entity store using:
- Python's built-in sqlite3 module for direct SQLite access
- Sync operations dispatched to thread pool via anyio
- Connection pooling with read/write separation
"""

from fxtr2.data.sqlite_store.sqlite_namespace import (
    SQLiteFxtrSession,
    SQLiteNamespaceArena,
    SQLiteNamespaceBackend,
)
from fxtr2.data.sqlite_store.sqlite_util import ConnectionPool, ConnectionPoolGroup

__all__ = [
    "SQLiteFxtrSession",
    "SQLiteNamespaceBackend",
    "SQLiteNamespaceArena",
    "ConnectionPool",
    "ConnectionPoolGroup",
]
