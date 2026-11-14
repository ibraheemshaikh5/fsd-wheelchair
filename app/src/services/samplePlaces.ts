import type { LatLng, Place } from '../types'
import { distanceM, offset } from '../lib/geo'

// Sample data for running without a Google key. [name, tags, bearing°, metres]
const SAMPLES: [string, string, number, number][] = [
  ['Uncommon Grounds', 'coffee cafe breakfast', 285, 610],
  ['Dunkin’', 'coffee donuts cafe', 250, 120],
  ['CVS Pharmacy', 'pharmacy drugstore store', 290, 380],
  ['TD Bank', 'bank atm', 275, 290],
  ['Pequossette Park', 'park playground', 330, 420],
  ['Gino’s Pizza', 'pizza restaurant food lunch dinner', 292, 470],
  ['Public Library', 'library books', 40, 540],
  ['Corner Grocery', 'grocery market food store', 120, 260],
  ['Town Hall', 'government office', 75, 760],
  ['Riverside Clinic', 'doctor clinic medical health', 200, 1100],
]

/** Sample places around `origin` whose name or tags match every typed word. */
export function sampleSearch(query: string, origin: LatLng): Place[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean)
  return SAMPLES.filter(([name, tags]) => {
    // Every typed word has to start some word of the name or its tags.
    const hay = `${name} ${tags}`.toLowerCase().split(/\s+/)
    return words.every((w) => hay.some((h) => h.startsWith(w)))
  }).map(([name, , bearing, dist]) => {
    const location = offset(origin, bearing, dist)
    return {
      id: `sample-${name}`,
      name,
      address: 'Sample place',
      location,
      distanceM: distanceM(origin, location),
    }
  })
}
