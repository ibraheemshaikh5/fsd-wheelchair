import type { LatLng, Place } from '../types'
import { HALF_MILE_M, boundsAround, distanceM } from '../lib/geo'
import { ensureGoogle } from '../lib/googleMaps'
import { sampleSearch } from './samplePlaces'

/** Destinations are limited to this straight-line distance from the chair. */
export const SEARCH_RADIUS_M = HALF_MILE_M

/** Places matching `query` within SEARCH_RADIUS_M of `origin`, nearest first. */
export async function searchPlaces(query: string, origin: LatLng): Promise<Place[]> {
  const q = query.trim()
  if (!q) return []
  const found = (await ensureGoogle()) ? await googleSearch(q, origin) : sampleSearch(q, origin)
  return found
    .filter((p) => p.distanceM <= SEARCH_RADIUS_M)
    .sort((a, b) => a.distanceM - b.distanceM)
}

/** A type-ahead match. Its location is only looked up once it is chosen. */
export interface PlaceSuggestion {
  id: string
  name: string
  address?: string
  distanceM: number | null
  resolve(): Promise<Place>
}

// One token per typing session, so Google bills the keystrokes plus the final
// lookup as a single request.
let session: google.maps.places.AutocompleteSessionToken | null = null

/** Suggestions for a partly typed name, within SEARCH_RADIUS_M of `origin`, nearest first. */
export async function suggestPlaces(input: string, origin: LatLng): Promise<PlaceSuggestion[]> {
  const q = input.trim()
  if (!q) return []
  if (!(await ensureGoogle())) {
    return sampleSearch(q, origin)
      .filter((p) => p.distanceM <= SEARCH_RADIUS_M)
      .sort((a, b) => a.distanceM - b.distanceM)
      .map((p) => ({ ...p, resolve: async () => p }))
  }
  session ??= new google.maps.places.AutocompleteSessionToken()
  const { suggestions } = await google.maps.places.AutocompleteSuggestion.fetchAutocompleteSuggestions({
    input: q,
    origin,
    locationRestriction: boundsAround(origin, SEARCH_RADIUS_M),
    sessionToken: session,
  })
  return suggestions
    .flatMap((s) => {
      const prediction = s.placePrediction
      // No distance means Google could not place it near the chair.
      if (!prediction || prediction.distanceMeters == null) return []
      if (prediction.distanceMeters > SEARCH_RADIUS_M) return []
      const name = prediction.mainText?.text ?? prediction.text.text
      return [
        {
          id: prediction.placeId,
          name,
          address: prediction.secondaryText?.text,
          distanceM: prediction.distanceMeters,
          resolve: async (): Promise<Place> => {
            const place = prediction.toPlace()
            await place.fetchFields({ fields: ['displayName', 'formattedAddress', 'location'] })
            session = null
            if (!place.location) throw new Error(`No location for ${name}`)
            const location = { lat: place.location.lat(), lng: place.location.lng() }
            return {
              id: prediction.placeId,
              name: place.displayName ?? name,
              address: place.formattedAddress ?? undefined,
              location,
              distanceM: distanceM(origin, location),
            }
          },
        },
      ]
    })
    .sort((a, b) => a.distanceM - b.distanceM)
}

async function googleSearch(query: string, origin: LatLng): Promise<Place[]> {
  // Text Search only restricts to a rectangle, so ask for the bounding box of the
  // circle and let the caller trim the corners.
  const { places } = await google.maps.places.Place.searchByText({
    textQuery: query,
    fields: ['id', 'displayName', 'formattedAddress', 'location'],
    locationRestriction: boundsAround(origin, SEARCH_RADIUS_M),
    maxResultCount: 12,
  })
  return places.flatMap((p) => {
    if (!p.location) return []
    const location = { lat: p.location.lat(), lng: p.location.lng() }
    return [
      {
        id: p.id,
        name: p.displayName ?? 'Unnamed place',
        address: p.formattedAddress ?? undefined,
        location,
        distanceM: distanceM(origin, location),
      },
    ]
  })
}
