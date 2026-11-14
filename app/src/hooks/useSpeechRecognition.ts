import { useCallback, useEffect, useRef, useState } from 'react'
import { useLatest } from './useLatest'

// Minimal typing for the Web Speech API, which TypeScript's DOM lib does not ship.
interface RecognitionResultEvent {
  resultIndex: number
  results: ArrayLike<ArrayLike<{ transcript: string }> & { isFinal: boolean }>
}
interface Recognition {
  lang: string
  interimResults: boolean
  continuous: boolean
  onresult: ((e: RecognitionResultEvent) => void) | null
  onerror: ((e: { error: string }) => void) | null
  onend: (() => void) | null
  start(): void
  abort(): void
}

function recognitionCtor(): (new () => Recognition) | undefined {
  const w = window as unknown as Record<string, new () => Recognition>
  return w.SpeechRecognition ?? w.webkitSpeechRecognition
}

export const speechSupported = Boolean(recognitionCtor())

/** One-shot dictation: start(), speak, and onFinal fires with the finished phrase. */
export function useSpeechRecognition(onFinal: (text: string) => void) {
  const [listening, setListening] = useState(false)
  const [transcript, setTranscript] = useState('')
  const [error, setError] = useState<string | null>(null)
  const recognition = useRef<Recognition | null>(null)
  const onFinalRef = useLatest(onFinal)

  const stop = useCallback(() => {
    recognition.current?.abort()
    recognition.current = null
    setListening(false)
  }, [])

  const start = useCallback(() => {
    const Ctor = recognitionCtor()
    if (!Ctor) return
    recognition.current?.abort()
    const rec = new Ctor()
    recognition.current = rec
    rec.lang = navigator.language || 'en-US'
    rec.interimResults = true
    rec.continuous = false
    let finalText = ''
    rec.onresult = (e) => {
      let text = ''
      for (let i = 0; i < e.results.length; i++) text += e.results[i][0].transcript
      setTranscript(text)
      // Safari does not always flag the last result as final, so keep the latest.
      finalText = text
    }
    rec.onerror = (e) => {
      if (e.error === 'aborted') return
      setError(
        e.error === 'not-allowed' || e.error === 'service-not-allowed'
          ? 'Microphone access is turned off for this site.'
          : e.error === 'no-speech'
            ? 'Didn’t catch that.'
            : 'Voice input stopped unexpectedly.',
      )
    }
    rec.onend = () => {
      if (recognition.current !== rec) return
      recognition.current = null
      setListening(false)
      if (finalText.trim()) onFinalRef.current(finalText.trim())
    }
    setTranscript('')
    setError(null)
    setListening(true)
    try {
      rec.start()
    } catch {
      setListening(false)
      setError('Voice input could not start.')
    }
  }, [onFinalRef])

  useEffect(() => stop, [stop])

  return { listening, transcript, error, start, stop }
}
