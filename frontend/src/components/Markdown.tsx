import { useMemo } from 'react'
import { renderMarkdown } from '../lib/markdown'

/** Rendered markdown, or a muted placeholder when there is none. */
export function Markdown({ source, empty }: { source: string; empty: string }) {
  const html = useMemo(() => renderMarkdown(source), [source])
  if (!source) return <span className="detail-none">{empty}</span>
  return <div className="markdown" dangerouslySetInnerHTML={{ __html: html }} />
}
