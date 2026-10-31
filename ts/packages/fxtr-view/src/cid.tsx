// A full cid as text with its short form emphasized: the common prefix and the tail are marked so a
// stylesheet can gray them out, and the run `shortCid` would show is marked to stand out. Wherever a
// short cid appears elsewhere, it is exactly that run.

import { splitCid } from "./ids";

export function CidText({ cid, className }: { readonly cid: string; readonly className?: string | undefined }) {
  const { prefix, short, rest } = splitCid(cid);
  return (
    <span className={className === undefined ? "fxtr-view-cid" : `fxtr-view-cid ${className}`} title={cid}>
      {prefix && <span className="fxtr-view-cid-prefix">{prefix}</span>}
      <span className="fxtr-view-cid-short">{short}</span>
      {rest && <span className="fxtr-view-cid-rest">{rest}</span>}
    </span>
  );
}
