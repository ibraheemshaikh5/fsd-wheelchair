import { useEffect } from 'react'
import { speechSupported, useSpeechRecognition } from '../../hooks/useSpeechRecognition'
import { Button } from '../Button'
import { Dialog } from '../Dialog'
import { MicIcon } from '../icons'

export interface VoiceQuestion {
  text: string
  yes: string
  no: string
  onYes: () => void
  onNo: () => void
}

interface Props {
  /** Shown instead of the transcript while the request is being resolved. */
  note: string | null
  /** A yes/no question to settle before continuing. */
  question: VoiceQuestion | null
  onFinal: (transcript: string) => void
  onClose: () => void
}

export function VoiceOverlay({ note, question, onFinal, onClose }: Props) {
  const { listening, transcript, error, start, stop } = useSpeechRecognition(onFinal)

  useEffect(() => {
    if (speechSupported) start()
    return stop
  }, [start, stop])

  const headline = !speechSupported
    ? 'Voice isn’t available in this browser'
    : (question?.text ??
      note ??
      (transcript || error || (listening ? 'Where to?' : 'Tap the microphone to try again')))

  return (
    <Dialog label="Voice destination" onDismiss={onClose} className="voice-sheet">
      <button
        type="button"
        className={`voice-mic ${listening ? 'listening' : ''}`}
        aria-label={listening ? 'Listening' : 'Start listening'}
        disabled={!speechSupported || listening || Boolean(note) || Boolean(question)}
        onClick={start}
      >
        <MicIcon size={40} />
      </button>
      <p className="voice-headline" aria-live="polite">
        {headline}
      </p>
      {question ? (
        <div className="voice-choices">
          <Button variant="primary" onClick={question.onYes}>
            {question.yes}
          </Button>
          <Button variant="quiet" onClick={question.onNo}>
            {question.no}
          </Button>
        </div>
      ) : (
        <>
          <p className="voice-sub">
            {speechSupported ? 'Try “go to the kitchen” or “the nearest coffee shop”' : 'Use the search bar instead.'}
          </p>
          <Button variant="quiet" onClick={onClose}>
            Cancel
          </Button>
        </>
      )}
    </Dialog>
  )
}
