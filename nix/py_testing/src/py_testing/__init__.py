"""py_testing — Python side of the fxtr nix-integration tests.

Hosts the pytest suite under ``tests/`` (which nix-builds flake outputs
and reads the results back with a fxtr2 layered-store reader) plus any
throwaway entity types those tests need.

TODO: add the type-id attribute that ``mkEntityFromPython`` is tested
against; see
``projects/fxtr_workstation/plans/phase0/part5_nix_workdir/stage1_nix_foundations.md``.
"""
