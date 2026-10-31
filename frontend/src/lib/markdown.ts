import DOMPurify from 'dompurify'
import { marked } from 'marked'

/**
 * Docstrings come from whatever experiment is loaded, so their markdown is sanitized before it
 * is handed to `dangerouslySetInnerHTML`.
 */
export function renderMarkdown(source: string): string {
  return DOMPurify.sanitize(marked.parse(source, { async: false }))
}
