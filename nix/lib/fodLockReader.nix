# fodLockReader — read pinned FOD output hashes from a `fod_lock.toml`.
#
# A standalone, helper-agnostic reader: configure it once with the lock file's
# location, and drop the resulting function in wherever you would otherwise have
# written `lib.fakeHash` — i.e. as the `outputHash` of any fixed-output
# derivation (`entityFromTsvPin`, a user `mkFxtrBuilder` FOD, …). It is *not*
# coupled to any FOD helper; its only job is "key -> pinned hash".
#
#   fodLock = lib.fxtr.fodLockReader { lockFile = ./fod_lock.toml; };
#   fxtr.entities.thePin = lib.entityFromTsvPin {
#     namespaceRoot = ./.;
#     key = "thePin";
#     outputHash = fodLock "thePin";
#   };
#
# The lock file (TOML) is read lazily: applying the reader to `{ lockFile }`
# does *not* touch the file — only calling the returned `key` function does. So
# `builtins.attrNames` over an attrset of FODs (e.g. `fxtr.preloads`) never
# forces a hash, which is what lets `fxtr_nix update-hashes` discover slots
# before the lock exists.
#
# A missing slot throws, naming the key + the fix command. The tool writes
# sentinel hashes for the slots it is (re)locking, so pure eval never needs to
# mint one.
{
  # Path to the lock file (e.g. `./fod_lock.toml`).
  lockFile,
}:
let
  lock = builtins.fromTOML (builtins.readFile lockFile);
  hashes = lock.hashes or { };
in
key:
hashes.${key} or (throw ''
  fxtr fod_lock: no hash recorded for FOD slot '${key}' in ${toString lockFile}.
  Run `fxtr_nix update-hashes` to compute it.
'')
