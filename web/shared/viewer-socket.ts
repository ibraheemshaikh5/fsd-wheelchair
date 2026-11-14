// The viewer's socket to the service: status messages as JSON text, camera
// frames as JPEG bytes. Reconnects with backoff on its own.

import { backoffDelayMs } from './backoff';
import { parseStatus, type ServiceStatus } from './status';

const BACKOFF_BASE_MS = 500;
const BACKOFF_MAX_MS = 5000;
/** Sent by the service when it already has as many viewers as it allows. */
export const CLOSE_TOO_MANY_VIEWERS = 4429;

export type ViewerConnection = { readonly kind: 'open' } | { readonly kind: 'lost'; readonly reason: string };

/** The parts of a browser WebSocket this module uses, so tests can pass a fake. */
export interface ViewerSocket {
  binaryType: BinaryType;
  onopen: ((event: Event) => void) | null;
  onmessage: ((event: MessageEvent) => void) | null;
  onclose: ((event: CloseEvent) => void) | null;
  close(): void;
}

export interface ViewerOptions {
  readonly url: string;
  readonly createSocket: (url: string) => ViewerSocket;
  readonly setTimer: (fn: () => void, ms: number) => number;
  readonly clearTimer: (id: number) => void;
  readonly onConnection: (state: ViewerConnection) => void;
  readonly onStatus: (status: ServiceStatus) => void;
  readonly onFrame: (jpeg: ArrayBuffer) => void;
}

export interface ViewerHandle {
  /** Closes the socket and stops reconnecting. */
  close(): void;
}

/** ws:// or wss:// to /ws/viewer on the page's own host. */
export function viewerSocketUrl(location: Pick<Location, 'protocol' | 'host'>): string {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  return `${scheme}://${location.host}/ws/viewer`;
}

function lostReason(code: number): string {
  return code === CLOSE_TOO_MANY_VIEWERS ? 'too many viewers open' : 'connection lost';
}

export function connectViewer(options: ViewerOptions): ViewerHandle {
  let attempt = 0;
  let timer: number | null = null;
  let socket: ViewerSocket | null = null;
  let stopped = false;

  const open = (): void => {
    timer = null;
    const current = options.createSocket(options.url);
    socket = current;
    current.binaryType = 'arraybuffer';
    current.onopen = () => options.onConnection({ kind: 'open' });
    current.onmessage = (event) => {
      // Only a message proves the service kept this socket: it may accept and
      // then close straight away (too many viewers), so onopen alone does not reset.
      attempt = 0;
      if (event.data instanceof ArrayBuffer) {
        options.onFrame(event.data);
      } else if (typeof event.data === 'string') {
        const status = parseStatus(event.data);
        if (status !== null) options.onStatus(status);
      }
    };
    current.onclose = (event) => {
      if (stopped || socket !== current) return;
      socket = null;
      options.onConnection({ kind: 'lost', reason: lostReason(event.code) });
      const delay = backoffDelayMs(attempt, BACKOFF_BASE_MS, BACKOFF_MAX_MS);
      attempt += 1;
      timer = options.setTimer(open, delay);
    };
  };

  open();
  return {
    close() {
      stopped = true;
      if (timer !== null) options.clearTimer(timer);
      timer = null;
      socket?.close();
      socket = null;
    },
  };
}
