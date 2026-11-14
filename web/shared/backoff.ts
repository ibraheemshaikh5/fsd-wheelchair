/** Reconnect delay: doubles with each failed attempt, up to a cap. */
export function backoffDelayMs(attempt: number, baseMs: number, maxMs: number): number {
  return Math.min(maxMs, baseMs * 2 ** attempt);
}
