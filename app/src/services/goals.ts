/**
 * Decides what a spoken request is asking for.
 *
 * indoor   a place or instruction inside the building ("go to the kitchen").
 *          Sent to the driving service as a language goal; no GPS route.
 * outdoor  somewhere to look up on the map ("the nearest coffee shop", an address).
 * unsure   nothing in the wording settles it. The caller searches the map and,
 *          if nothing turns up, asks the rider.
 *
 * This is a wording heuristic so the flow works today. It is the one function to
 * replace when the driving service (or a language model) can classify requests.
 */
export type SpokenGoal =
  | { kind: 'indoor'; instruction: string }
  | { kind: 'outdoor' | 'unsure'; query: string }

const FILLER = /^(please\s+)?(can you\s+|could you\s+)?(take|bring|drive|get|navigate|go|head)(\s+me)?\s+(over\s+)?to\s+/i

const OUTDOOR =
  /\b(\d+\s+\w+|street|st|road|rd|avenue|ave|boulevard|blvd|lane|highway|square|nearest|closest|nearby|near me|store|shop|cafe|coffee|restaurant|pizza|pharmacy|grocery|market|park|bank|atm|station|bus stop|library|school|hospital|clinic|gym|church|post office|town hall|mall)\b/i

const INDOOR =
  /\b(kitchen|bathroom|restroom|toilet|bedroom|living room|dining room|laundry|hallway|hall|corridor|lobby|elevator|lift|stairs|upstairs|downstairs|closet|garage|room|office|door|window|couch|sofa|table|desk|bed|fridge|refrigerator|sink|counter|tv|television|charger|exit|entrance)\b/i

// Movement instructions rather than destinations.
const INSTRUCTION =
  /^(turn|go|move|back up|reverse|come|follow|stop|wait|keep going|straight|forward|left|right)\b|\b(follow me|come here|over there|this way|that way)\b/i

export function classifySpokenGoal(transcript: string): SpokenGoal {
  const said = transcript.trim().replace(/[.?!]+$/, '')
  const target = said.replace(FILLER, '')
  const query = target.replace(/^(the|a|an|my)\s+/i, '').replace(/^(nearest|closest|nearby)\s+/i, '')

  if (OUTDOOR.test(target)) return { kind: 'outdoor', query }
  if (INDOOR.test(target) || INSTRUCTION.test(target)) return { kind: 'indoor', instruction: said }
  return { kind: 'unsure', query }
}
