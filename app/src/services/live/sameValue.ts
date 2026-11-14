// Structural equality for the plain data in DriveState (numbers, strings, plain
// objects, arrays). Statuses arrive about 30 times a second and most change
// nothing the rider sees; comparing lets the service skip re-rendering the app.

const isPlainObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)

function sameArray(a: readonly unknown[], b: readonly unknown[]): boolean {
  return a.length === b.length && a.every((item, i) => sameValue(item, b[i]))
}

function sameObject(a: Record<string, unknown>, b: Record<string, unknown>): boolean {
  const keys = Object.keys(a)
  if (keys.length !== Object.keys(b).length) return false
  return keys.every((key) => Object.hasOwn(b, key) && sameValue(a[key], b[key]))
}

/** True when `a` and `b` hold the same data. The same reference is equal without looking inside. */
export function sameValue(a: unknown, b: unknown): boolean {
  if (Object.is(a, b)) return true
  if (Array.isArray(a) && Array.isArray(b)) return sameArray(a, b)
  if (isPlainObject(a) && isPlainObject(b)) return sameObject(a, b)
  return false
}
