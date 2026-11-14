import type { LatLng, Place } from '../types'
import { boundsAround, distanceM } from '../lib/geo'
import { ensureGoogle } from '../lib/googleMaps'
import { sampleSearch } from './samplePlaces'

/** Destinations are limited to this straight-line distance from the chair. */
export const SEARCH_RADIUS_M = 804.67 // half a mile

/** A type-ahead match. Its location is only looked up once it is chosen. */
export interface PlaceSuggestion {
  id: string
  name: string
  address?: string
  distanceM: number | null
  resolve(): Promise<Place>
}

/** A place that is already fully known, as a suggestion that resolves to itself. */
export const asSuggestion = (p: Place): PlaceSuggestion => ({ ...p, resolve: async () => p })

/** The items within SEARCH_RADIUS_M, nearest first, as a new array. */
const nearbyFirst = <T extends { distanceM: number }>(xs: T[]): T[] =>
  xs.filter((p) => p.distanceM <= SEARCH_RADIUS_M).sort((a, b) => a.distanceM - b.distanceM)

/** Places matching `query` within SEARCH_RADIUS_M of `origin`, nearest first. */
export async function searchPlaces(query: string, origin: LatLng): Promise<Place[]> {
  const q = query.trim()
  if (!q) return []
  const found = (await ensureGoogle()) ? await googleSearch(q, origin) : sampleSearch(q, origin)
  return nearbyFirst(found)
}

// One token per typing session, so Google bills the keystrokes plus the final
// lookup as a single request.
let session: google.maps.places.AutocompleteSessionToken | null = null

/** Suggestions for a partly typed name, within SEARCH_RADIUS_M of `origin`, nearest first. */
export async function suggestPlaces(input: string, origin: LatLng): Promise<PlaceSuggestion[]> {
  const q = input.trim()
  if (!q) return []
  if (!(await ensureGoogle())) return nearbyFirst(sampleSearch(q, origin)).map(asSuggestion)
  session ??= new google.maps.places.AutocompleteSessionToken()
  const { suggestions } = await google.maps.places.AutocompleteSuggestion.fetchAutocompleteSuggestions({
    input: q,
    origin,
    locationRestriction: boundsAround(origin, SEARCH_RADIUS_M),
    sessionToken: session,
  })
  return nearbyFirst(
    suggestions.flatMap((s) => {
      const prediction = s.placePrediction
      // No distance means Google could not place it near the chair.
      if (!prediction || prediction.distanceMeters == null) return []
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
            const found = fromGooglePlace(place, origin, prediction.placeId, name)
            if (!found) throw new Error(`No location for ${name}`)
            return found
          },
        },
      ]
    }),
  )
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
  return places.flatMap((p) => fromGooglePlace(p, origin, p.id, 'Unnamed place') ?? [])
}

/**
 * A Google Place as an app Place, or null if Google gave no location.
 * The id is passed in so suggestions keep the prediction's id.
 */
function fromGooglePlace(
  p: google.maps.places.Place,
  origin: LatLng,
  id: string,
  fallbackName: string,
): Place | null {
  if (!p.location) return null
  const location = { lat: p.location.lat(), lng: p.location.lng() }
  return {
    id,
    name: p.displayName ?? fallbackName,
    address: p.formattedAddress ?? undefined,
    location,
    distanceM: distanceM(origin, location),
  }
}
