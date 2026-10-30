"""Deprecated in-band-bytes sandbox-bundle path (WASM ``prepare`` + bundle
materialization; the Docker / py_container halves were removed in stage 8).

Superseded by the git-referenced service path (``NixWasmServiceDefn`` +
``fxtr_sandboxing.nix`` + ``mkWasmService``); kept working for the still-live
``prepare_package_bundle`` builtin (removed in a later stage). See
``projects/fxtr_workstation/plans/phase0/part5_nix_workdir/design_notes/12_nix_drv_reference.md``.
"""
