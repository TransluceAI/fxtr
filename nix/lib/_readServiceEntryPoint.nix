# _readServiceEntryPoint — read the single fxtr.service entry point from a
# guest package's pyproject.toml.
#
# Shared by mkWasmService + mkContainerService. Same convention as the
# deprecated `prepare.read_entry_point`: a single
# `[project.entry-points."fxtr.service"]` entry whose value is a
# `module:function` string. The key name is arbitrary; we take the sole
# value and fail loudly on a missing / ambiguous declaration.
#
# `helper` names the calling helper for the error message.
{ src, helper }:
let
  pyproject = builtins.fromTOML (builtins.readFile (src + "/pyproject.toml"));
  serviceEntries = pyproject.project."entry-points"."fxtr.service" or { };
  entryNames = builtins.attrNames serviceEntries;
in
if builtins.length entryNames == 1 then
  serviceEntries.${builtins.head entryNames}
else
  throw (
    "${helper}: expected exactly one "
    + "[project.entry-points.\"fxtr.service\"] entry in "
    + "${toString src}/pyproject.toml, found ${toString (builtins.length entryNames)}"
  )
