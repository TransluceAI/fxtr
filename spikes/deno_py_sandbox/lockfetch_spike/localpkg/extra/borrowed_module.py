"""Inside the member dir (out-of-dir force-include is unsupported for LOCAL
members — uv2nix stages only the member dir; git-sourced deps build from the
full fetched repo, so fxtr2-portable's ../fxtr2 pattern still works there)."""

BORROWED = "force-include works"
