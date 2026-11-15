import type { Level } from '../shared/status';
import {
  CLOSE_IDLE,
  CLOSE_NO_REPLY,
  CLOSE_POLICY_VIOLATION,
  CLOSE_REPLACED,
  type TransportState,
} from './transport';

export interface Described {
  readonly text: string;
  readonly level: Level;
}

export const TAKEOVER_TEXT = 'Another phone or tab took over the stream. Tap Start to resume.';

function describeStopped(state: TransportState): Described {
  switch (state.lastClose?.code) {
    case CLOSE_REPLACED:
      return { text: TAKEOVER_TEXT, level: 'bad' };
    default:
      return { text: 'Stopped', level: 'none' };
  }
}

function describeWaiting(state: TransportState): Described {
  const retrying = `retrying (attempt ${state.attempt})`;
  switch (state.lastClose?.code) {
    // Only sent after too many invalid messages. A foreign origin is refused at
    // the handshake (HTTP 403), which the browser reports as 1006.
    case CLOSE_POLICY_VIOLATION:
      return { text: "The service refused this page's messages (see service log); retrying", level: 'bad' };
    case CLOSE_IDLE:
      return { text: `The service got no data from this phone for a while; ${retrying}`, level: 'warn' };
    case CLOSE_NO_REPLY:
      return { text: `No reply from the service; ${retrying}`, level: 'warn' };
    default:
      return { text: `Connection lost, ${retrying}`, level: 'warn' };
  }
}

export function describeConnection(state: TransportState): Described {
  switch (state.status) {
    case 'open':
      return { text: 'Connected', level: 'ok' };
    case 'connecting':
      return { text: 'Connecting…', level: 'warn' };
    case 'stopped':
      return describeStopped(state);
    case 'waiting':
      return describeWaiting(state);
  }
}

/** Explain why the camera could not start, in words the person holding the phone can act on. */
export function describeStartError(error: unknown): string {
  if (error instanceof DOMException) {
    if (error.name === 'NotAllowedError') {
      return 'Camera permission denied. Allow camera access for this site in Safari (aA menu › Website Settings) and tap Start again.';
    }
    if (error.name === 'NotFoundError' || error.name === 'OverconstrainedError') {
      return 'No usable rear camera was found.';
    }
  }
  return error instanceof Error ? error.message : 'Unexpected error starting the camera.';
}
