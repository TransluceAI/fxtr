"""Script-form builder for the ``mkFxtrScriptBuilder`` nix round-trip test.

``mkFxtrScriptBuilder`` runs this file as ``__main__`` via ``${venv}/bin/python
${script}``; it imports the shared ``build`` entrypoint from the py_testing
package (present in the venv) and drives it through ``nix_builder.main``. The
file itself is copied into the store as a single file — everything it needs is
imported from the venv (the sibling-file caveat in ``mkFxtrScriptBuilder.nix``).
"""

from __future__ import annotations

from fxtr2.data.layered_file_store import nix_builder
from py_testing.builder_fixture import build

if __name__ == "__main__":
    nix_builder.main(build)
