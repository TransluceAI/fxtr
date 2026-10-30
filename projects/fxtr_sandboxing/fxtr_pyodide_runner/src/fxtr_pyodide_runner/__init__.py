"""Host-side accessors for the pyodide sandbox backend's driver files.

The deno driver (``main.ts`` + ``deno.json``) is host-owned and trusted: it
ships as data files of this package and versions with the host fxtr install.
The spawner passes its path to ``deno run`` directly — it is never part of
the flake-built runtime bundle. (``guest_bootstrap.py`` is the opposite:
flake-copied into the runtime bundle by ``_mkPyodideRuntimeBundle.nix`` and
run inside pyodide; the accessor here exists only for the nix-side copy and
tests.)
"""

from __future__ import annotations

from pathlib import Path

_PKG_DIR = Path(__file__).parent


def driver_main_ts_path() -> Path:
    """Path to the deno driver entry point (``main.ts``)."""
    return _PKG_DIR / "main.ts"


def driver_config_path() -> Path:
    """Path to the driver's ``deno.json``.

    Load-bearing beyond configuration: without a config file next to the
    driver, deno's config discovery walks up from the entry module and may
    find (and try to migrate) unrelated workspace config.
    """
    return _PKG_DIR / "deno.json"


def guest_bootstrap_path() -> Path:
    """Path to the pyodide guest bootstrap source (flake-copied at build)."""
    return _PKG_DIR / "guest_bootstrap.py"
