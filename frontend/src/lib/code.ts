import { REPO_ROOT_OVERRIDE } from '../config'
import type { SourceInfo } from '../types'
import type { Editor } from './editor'

/** The function's file as an absolute path on the editor's machine, at its first line. */
export function filePath(source: SourceInfo): string {
  const root = (REPO_ROOT_OVERRIDE ?? source.repoRoot).replace(/\/$/, '')
  return `${root}/${source.file}:${source.startLine}`
}

/** A link that opens the function in `editor`, at the first line of its definition. */
export function editorHref(editor: Editor, source: SourceInfo): string {
  return `${editor.scheme}://file${filePath(source)}`
}

export function codeLocation(source: SourceInfo): string {
  const range = source.endLine !== source.startLine ? `–${source.endLine}` : ''
  return `${source.file}:${source.startLine}${range}`
}

/** A commit hash as the viewer shows it. */
export const shortCommit = (commit: string): string => commit.slice(0, 7)
