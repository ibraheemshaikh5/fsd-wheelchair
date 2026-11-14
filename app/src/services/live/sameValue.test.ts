import { describe, expect, it } from 'vitest'
import { sameValue } from './sameValue'

describe('sameValue', () => {
  it('compares primitives, treating NaN as itself', () => {
    expect(sameValue(1, 1)).toBe(true)
    expect(sameValue('a', 'b')).toBe(false)
    expect(sameValue(Number.NaN, Number.NaN)).toBe(true)
    expect(sameValue(null, undefined)).toBe(false)
    expect(sameValue(0, null)).toBe(false)
  })

  it('compares plain objects and arrays by value', () => {
    expect(sameValue({ a: 1, b: [1, { c: 2 }] }, { a: 1, b: [1, { c: 2 }] })).toBe(true)
    expect(sameValue({ a: 1, b: [1, { c: 2 }] }, { a: 1, b: [1, { c: 3 }] })).toBe(false)
    expect(sameValue([1, 2], [1, 2, 3])).toBe(false)
    expect(sameValue({ a: 1 }, { a: 1, b: 2 })).toBe(false)
    expect(sameValue({ a: 1, b: undefined }, { a: 1, c: undefined })).toBe(false)
    expect(sameValue([1], { 0: 1 })).toBe(false)
  })

  it('short-circuits on the same reference', () => {
    const big = { points: Array.from({ length: 1000 }, (_, i) => ({ lat: i, lng: i })) }
    expect(sameValue(big, big)).toBe(true)
  })
})
