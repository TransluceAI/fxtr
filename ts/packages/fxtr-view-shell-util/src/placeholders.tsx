// The default fallbacks dispatch shows when a host passes none of its own: minimal spans a shell
// styles through their class names.

export function PlaceholderLoading() {
  return <span className="fxtr-view-loading">…</span>;
}

export function PlaceholderError({ error }: { readonly error: unknown }) {
  return (
    <span className="fxtr-view-error" role="alert">
      {error instanceof Error ? error.message : String(error)}
    </span>
  );
}

export function PlaceholderNoRenderer({ subjectType }: { readonly subjectType: string }) {
  return <span className="fxtr-view-no-match">no view for {subjectType}</span>;
}
