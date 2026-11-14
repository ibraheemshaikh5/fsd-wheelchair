import { useEffect, useRef, useState, type FormEvent } from 'react'
import type { LatLng, Place } from '../../types'
import { useLatest } from '../../hooks/useLatest'
import { distanceLabel } from '../../lib/format'
import { classifySpokenGoal } from '../../services/goals'
import { searchPlaces, suggestPlaces, type PlaceSuggestion } from '../../services/places'
import { CloseIcon, MicIcon, PinIcon, SearchIcon } from '../icons'
import { VoiceOverlay, type VoiceQuestion } from './VoiceOverlay'

const SUGGEST_DELAY_MS = 150

interface Props {
  origin: LatLng
  /** Sets the places pinned on the map. */
  onResults: (places: Place[]) => void
  onSelect: (place: Place) => void
  /** A spoken request for somewhere inside the building. */
  onIndoorGoal: (instruction: string) => void
  /** Set while a route to the chosen place is being planned. */
  pendingId: string | null
  error: string | null
}

type Status = 'empty' | 'loading' | 'done' | 'failed'

/** One row under the search bar: a type-ahead suggestion or a full search result. */
interface Row {
  id: string
  name: string
  address?: string
  distanceM: number | null
  pick: () => Promise<Place>
}

const fromSuggestion = (s: PlaceSuggestion): Row => ({ ...s, pick: s.resolve })
const fromPlace = (p: Place): Row => ({ ...p, pick: async () => p })

export function SearchPanel({ origin, onResults, onSelect, onIndoorGoal, pendingId, error }: Props) {
  const [query, setQuery] = useState('')
  const [rows, setRows] = useState<Row[]>([])
  const [status, setStatus] = useState<Status>('empty')
  const [pickingId, setPickingId] = useState<string | null>(null)
  const [pickError, setPickError] = useState<string | null>(null)
  const [voiceOpen, setVoiceOpen] = useState(false)
  const [voiceNote, setVoiceNote] = useState<string | null>(null)
  const [voiceQuestion, setVoiceQuestion] = useState<VoiceQuestion | null>(null)
  const latestRequest = useRef(0)
  const suggestTimer = useRef<ReturnType<typeof setTimeout>>(undefined)
  const originRef = useLatest(origin)
  const busyId = pickingId ?? pendingId

  useEffect(() => () => clearTimeout(suggestTimer.current), [])

  /** Runs one lookup, ignoring its answer if a newer one has started since. */
  async function load<T>(lookup: () => Promise<T[]>, toRow: (item: T) => Row): Promise<T[] | null> {
    const id = ++latestRequest.current
    setStatus('loading')
    setPickError(null)
    try {
      const items = await lookup()
      if (id !== latestRequest.current) return null
      setRows(items.map(toRow))
      setStatus('done')
      return items
    } catch (e) {
      console.error('Place search failed', e)
      if (id !== latestRequest.current) return null
      setRows([])
      setStatus('failed')
      return null
    }
  }

  /** Full search: every match, pinned on the map. */
  async function search(text: string): Promise<Place[] | null> {
    clearTimeout(suggestTimer.current)
    const places = await load(() => searchPlaces(text, originRef.current), fromPlace)
    if (places) onResults(places)
    return places
  }

  /** The rider edited the text: show suggestions for it after a short pause. */
  function type(text: string) {
    setQuery(text)
    onResults([])
    clearTimeout(suggestTimer.current)
    if (!text.trim()) {
      latestRequest.current++
      setRows([])
      setStatus('empty')
      return
    }
    suggestTimer.current = setTimeout(
      () => void load(() => suggestPlaces(text, originRef.current), fromSuggestion),
      SUGGEST_DELAY_MS,
    )
  }

  function submit(e: FormEvent) {
    e.preventDefault()
    if (query.trim()) void search(query)
  }

  async function pick(row: Row) {
    setPickingId(row.id)
    setPickError(null)
    try {
      onSelect(await row.pick())
    } catch (e) {
      console.error('Place lookup failed', e)
      setPickError(`Couldn’t look up ${row.name}. Try again.`)
    } finally {
      setPickingId(null)
    }
  }

  function closeVoice() {
    setVoiceOpen(false)
    setVoiceNote(null)
    setVoiceQuestion(null)
  }

  async function handleSpeech(transcript: string) {
    const goal = classifySpokenGoal(transcript)
    if (goal.kind === 'indoor') {
      closeVoice()
      onIndoorGoal(goal.instruction)
      return
    }
    setQuery(goal.query)
    setVoiceNote(`Finding “${goal.query}”…`)
    const places = await search(goal.query)
    setVoiceNote(null)
    if (places?.length) {
      // Nearest match becomes the destination; the rider still has to press Start.
      closeVoice()
      onSelect(places[0])
    } else if (goal.kind === 'unsure' && places) {
      // Not on the map and the wording didn't say: let the rider decide.
      setVoiceQuestion({
        text: `Is “${goal.query}” inside this building?`,
        yes: 'Yes, it’s inside',
        no: 'No, search the map',
        onYes: () => {
          closeVoice()
          onIndoorGoal(transcript.trim())
        },
        onNo: closeVoice,
      })
    } else {
      closeVoice()
    }
  }

  const problem = pickError ?? error

  return (
    <>
      <section className="card search-card" aria-label="Choose a destination">
        <form className="search-row" role="search" onSubmit={submit}>
          <SearchIcon />
          <input
            type="search"
            inputMode="search"
            enterKeyHint="search"
            placeholder="Navigate"
            aria-label="Search for a destination"
            autoComplete="off"
            autoCorrect="off"
            spellCheck={false}
            value={query}
            onChange={(e) => type(e.target.value)}
          />
          {query && (
            <button type="button" className="icon-btn" aria-label="Clear search" onClick={() => type('')}>
              <CloseIcon />
            </button>
          )}
          <button
            type="button"
            className="icon-btn mic-btn"
            aria-label="Say a destination"
            onClick={() => setVoiceOpen(true)}
          >
            <MicIcon />
          </button>
        </form>

        {status === 'failed' && <p className="search-hint search-error">Search isn’t working right now. Try again.</p>}
        {status === 'done' && rows.length === 0 && (
          <p className="search-hint">Nothing within 0.5 mi matches “{query.trim()}”.</p>
        )}
        {problem && <p className="search-hint search-error">{problem}</p>}

        {rows.length > 0 && (
          <ul className="results">
            {rows.map((row) => (
              <li key={row.id}>
                <button type="button" className="result" disabled={busyId !== null} onClick={() => void pick(row)}>
                  <span className="result-pin">
                    <PinIcon />
                  </span>
                  <span className="result-text">
                    <span className="result-name">{row.name}</span>
                    {row.address && <span className="result-address">{row.address}</span>}
                  </span>
                  <span className="result-distance">
                    {busyId === row.id ? 'Routing…' : row.distanceM == null ? '' : distanceLabel(row.distanceM)}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      {voiceOpen && (
        <VoiceOverlay note={voiceNote} question={voiceQuestion} onFinal={handleSpeech} onClose={closeVoice} />
      )}
    </>
  )
}
