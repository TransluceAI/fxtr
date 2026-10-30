/**
 * Shared helper functions for renderers.
 */

/**
 * Get the theme color for an entity type.
 */
export function getTypeColor(type: string): string {
  const colors: Record<string, string> = {
    collection: '#f0a',
    transcript: '#0af',
    turn: '#0fa',
    judgement_set: '#fa0',
    judgement: '#ff0',
    rubric: '#a0f',
    misconfigured: '#f00',
  };
  return colors[type] ?? '#888';
}

/**
 * Truncate a string in the middle with ellipsis.
 */
export function truncateMiddle(str: string, maxLength: number): string {
  if (str.length <= maxLength) return str;
  const half = Math.floor((maxLength - 3) / 2);
  return str.slice(0, half) + '...' + str.slice(-half);
}

/**
 * Truncate a string at the end with ellipsis.
 */
export function truncateEnd(str: string, maxLength: number): string {
  if (str.length <= maxLength) return str;
  return str.slice(0, maxLength - 3) + '...';
}
