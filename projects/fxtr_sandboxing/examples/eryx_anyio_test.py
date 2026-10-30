"""Demonstrate that anyio cannot import inside an eryx sandbox,
and test whether a socket shim fixes it.

The failure is:
    ImportError: cannot import name 'AddressFamily' from 'socket'

eryx's WASM sandbox provides a limited `socket` module that doesn't
include AddressFamily, SocketKind, or the socket class. anyio imports
these unconditionally in anyio.abc._eventloop.

The shim approach: patch the sandbox's socket module to add stub
versions of the missing names before importing anyio. Since anyio
uses `from __future__ import annotations`, the type annotations are
strings at runtime and the stubs only need to exist as importable
names, not actually work.
"""

import subprocess
import tempfile

import eryx

SOCKET_SHIM = """
# Patch eryx's socket module with missing names that anyio/httpcore need at import time.
# eryx's WASM socket module has the AF_*/SOCK_* constants as plain ints but is missing
# the IntEnum wrappers (AddressFamily, SocketKind) and some constants (AF_UNSPEC).
import socket as _socket
import enum as _enum

if not hasattr(_socket, 'AF_UNSPEC'):
    _socket.AF_UNSPEC = 0

if not hasattr(_socket, 'AddressFamily'):
    class AddressFamily(_enum.IntEnum):
        AF_UNSPEC = 0
        AF_INET = _socket.AF_INET
        AF_INET6 = _socket.AF_INET6
        AF_UNIX = _socket.AF_UNIX
    _socket.AddressFamily = AddressFamily

if not hasattr(_socket, 'SocketKind'):
    class SocketKind(_enum.IntEnum):
        SOCK_STREAM = _socket.SOCK_STREAM
        SOCK_DGRAM = _socket.SOCK_DGRAM
    _socket.SocketKind = SocketKind

del _enum
"""


def _make_site_packages_with_anyio() -> str:
    """Install anyio into a temp directory for use as eryx site_packages."""
    site_dir = tempfile.mkdtemp(prefix="eryx_anyio_test_")
    subprocess.run(
        ["uv", "pip", "install", "--target", site_dir, "anyio"],
        check=True,
        capture_output=True,
    )
    return site_dir


def main():
    print("Installing anyio into temp site_packages...")
    site_dir = _make_site_packages_with_anyio()

    factory = eryx.SandboxFactory(site_packages=site_dir)
    sandbox = factory.create_sandbox(
        on_stdout=lambda chunk: print(chunk, end=""),
        on_stderr=lambda chunk: print(f"[stderr] {chunk}", end=""),
    )

    # Show what the socket module looks like in eryx
    print("=== socket module contents in eryx ===")
    sandbox.execute(
        "import socket; print(sorted(name for name in dir(socket) if not name.startswith('_')))"
    )

    # Attempt 1: import anyio without shim (will fail)
    print("\n=== attempt 1: import anyio without shim ===")
    try:
        sandbox.execute("import anyio; print(f'anyio {anyio.__version__} imported OK')")
    except eryx.ExecutionError as e:
        print(f"\nFailed as expected: {type(e).__name__}")

    # Attempt 2: apply socket shim, then import anyio
    print("\n=== attempt 2: apply socket shim, then import anyio ===")
    sandbox2 = factory.create_sandbox(
        on_stdout=lambda chunk: print(chunk, end=""),
        on_stderr=lambda chunk: print(f"[stderr] {chunk}", end=""),
    )
    try:
        sandbox2.execute(SOCKET_SHIM + "\nimport anyio; print('anyio imported OK')")
        print("\nSocket shim works!")
    except eryx.ExecutionError as e:
        print(f"\nStill failed: {e}")


if __name__ == "__main__":
    main()
