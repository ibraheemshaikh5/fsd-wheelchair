import { describe, expect, it } from 'vitest'
import { classifySpokenGoal } from './goals'

describe('classifySpokenGoal', () => {
  it('keeps "the nearest bathroom" indoors, since the noun outranks "nearest"', () => {
    expect(classifySpokenGoal('Take me to the nearest bathroom.')).toEqual({
      kind: 'indoor',
      instruction: 'Take me to the nearest bathroom',
    })
  })

  it('sends "the nearest coffee shop" to the map without the qualifier', () => {
    expect(classifySpokenGoal('nearest coffee shop')).toEqual({ kind: 'outdoor', query: 'coffee shop' })
  })

  it('treats a room as an indoor goal', () => {
    expect(classifySpokenGoal('go to the kitchen')).toEqual({ kind: 'indoor', instruction: 'go to the kitchen' })
  })

  it('sends "nearest" plus an unknown name to the map', () => {
    expect(classifySpokenGoal('nearest Walgreens')).toEqual({ kind: 'outdoor', query: 'Walgreens' })
  })

  it('sends a street address to the map', () => {
    expect(classifySpokenGoal('take me to 123 Main Street')).toEqual({ kind: 'outdoor', query: '123 Main Street' })
  })

  it('keeps "post office" outdoors although "office" is an indoor word', () => {
    expect(classifySpokenGoal('the nearest post office').kind).toBe('outdoor')
  })

  it('treats a movement instruction as indoor', () => {
    expect(classifySpokenGoal('turn left').kind).toBe('indoor')
  })

  it('is unsure about a bare name', () => {
    expect(classifySpokenGoal('take me to Grandma’s')).toEqual({ kind: 'unsure', query: 'Grandma’s' })
  })
})
