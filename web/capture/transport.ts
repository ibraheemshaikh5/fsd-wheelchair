import { backoffDelayMs } from '../shared/backoff';
import { parseServerMessage, type ServerMessage } from '../shared/protocol';

export const SOCKET_OPEN = 1;

// Close codes; see docs/phone-protocol.md and service/drive_service/sockets.py.
/** A newer phone connection replaced this one. Reconnecting would just kick it back off. */
export const CLOSE_REPLACED = 4410;
/** The service heard nothing from this phone for its idle timeout. */
export const CLOSE_IDLE = 4408;
export const CLOSE_POLICY_VIOLATION = 1008;
/** Used locally when the phone gives up on a link that has gone silent. */
export const CLOSE_NO_REPLY = 4000;
export const NO_REPLY_REASON = 'no reply from service';

/** The parts of a WebSocket this uses, so tests can supply a fake. */
export interface SocketLike {
  readonly readyState: number;
  readonly bufferedAmount: number;
  binaryType: BinaryType;
  send(data: string | Uint8Array<ArrayBuffer>): void;
  close(): void;
  onopen: (() => void) | null;
  onclose: ((event: { code: number; reason: string }) => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
  onerror: (() => void) | null;
}

export interface CloseInfo {
  readonly code: number;
  readonly reason: string;
}

export interface TransportState {
  readonly status: 'connecting' | 'open' | 'waiting' | 'stopped';
  /** Failed connection attempts since the last successful one. */
  readonly attempt: number;
  readonly lastClose: CloseInfo | null;
}

export interface TransportOptions {
  readonly createSocket: (url: string) => SocketLike;
  readonly setTimer: (fn: () => void, ms: number) => number;
  readonly clearTimer: (id: number) => void;
  readonly onState: (state: TransportState) => void;
  readonly onMessage: (message: ServerMessage) => void;
  /** Skip frames and droppable messages while more than this many bytes are queued in the socket. */
  readonly maxBufferedBytes: number;
  /** Wall clock in ms, for liveness and backoff decisions. */
  readonly now: () => number;
}

const BACKOFF_BASE_MS = 500;
const BACKOFF_MAX_MS = 10_000;
/** A socket open at least this long, or one the service has answered on, resets the backoff. */
const STABLE_AFTER_MS = 2000;

/** Latest-wins: a stale frame is worse than a missing one, so never queue behind a slow link. */
export function shouldSendFrame(bufferedAmount: number, limitBytes: number): boolean {
  return bufferedAmount < limitBytes;
}

/** One WebSocket to the service that reconnects with backoff until stopped. */
export class Transport {
  private socket: SocketLike | null = null;
  private timer: number | null = null;
  private state: TransportState = { status: 'stopped', attempt: 0, lastClose: null };
  private openedAtMs = 0;
  private lastInboundMs = 0;
  private answered = false;

  constructor(
    private readonly url: string,
    private readonly options: TransportOptions,
  ) {}

  start(): void {
    if (this.state.status !== 'stopped') return;
    this.connect(0, null);
  }

  stop(): void {
    if (this.timer !== null) this.options.clearTimer(this.timer);
    this.timer = null;
    const socket = this.socket;
    this.socket = null;
    socket?.close();
    this.setState({ ...this.state, status: 'stopped' });
  }

  /**
   * Send a JSON message. A droppable one is skipped while the socket is backed
   * up, like a frame; the rest (hello, ping, gps, imu) always go out when open.
   */
  sendJson(message: unknown, droppable = false): boolean {
    const socket = this.openSocket();
    if (socket === null) return false;
    if (droppable && !shouldSendFrame(socket.bufferedAmount, this.options.maxBufferedBytes)) return false;
    socket.send(JSON.stringify(message));
    return true;
  }

  /**
   * Give up on an open socket the service has not answered on for timeoutMs
   * and reconnect. A dead link can otherwise look open for a long time.
   */
  checkLiveness(nowMs: number, timeoutMs: number): void {
    if (this.state.status !== 'open' || nowMs - this.lastInboundMs <= timeoutMs) return;
    const socket = this.socket;
    // Detach first so the late close event from this socket is ignored.
    this.socket = null;
    socket?.close();
    this.scheduleReconnect({ code: CLOSE_NO_REPLY, reason: NO_REPLY_REASON });
  }

  sendFrame(bytes: Uint8Array<ArrayBuffer>): boolean {
    const socket = this.openSocket();
    if (socket === null || !shouldSendFrame(socket.bufferedAmount, this.options.maxBufferedBytes)) {
      return false;
    }
    socket.send(bytes);
    return true;
  }

  private openSocket(): SocketLike | null {
    return this.socket !== null && this.socket.readyState === SOCKET_OPEN ? this.socket : null;
  }

  private connect(attempt: number, lastClose: CloseInfo | null): void {
    this.timer = null;
    const socket = this.options.createSocket(this.url);
    socket.binaryType = 'arraybuffer';
    this.socket = socket;
    this.setState({ status: 'connecting', attempt, lastClose });

    socket.onopen = () => {
      if (this.socket !== socket) return;
      const now = this.options.now();
      this.openedAtMs = now;
      this.lastInboundMs = now;
      this.answered = false;
      // Keep the attempt count: a service that accepts and then closes at once still backs off.
      this.setState({ status: 'open', attempt: this.state.attempt, lastClose: this.state.lastClose });
    };
    socket.onmessage = (event) => {
      if (this.socket !== socket) return;
      this.lastInboundMs = this.options.now();
      this.answered = true;
      if (typeof event.data !== 'string') return;
      const message = parseServerMessage(event.data);
      if (message !== null) this.options.onMessage(message);
    };
    socket.onclose = (event) => {
      if (this.socket !== socket) return;
      this.socket = null;
      const lastClose = { code: event.code, reason: event.reason };
      if (event.code === CLOSE_REPLACED) {
        this.setState({ status: 'stopped', attempt: 0, lastClose });
        return;
      }
      this.scheduleReconnect(lastClose);
    };
    // A failed connection is followed by close, which handles the retry.
    socket.onerror = () => {};
  }

  /** Whether the socket that just closed counted as a successful connection. */
  private wasStable(): boolean {
    if (this.state.status !== 'open') return false;
    return this.answered || this.options.now() - this.openedAtMs >= STABLE_AFTER_MS;
  }

  private scheduleReconnect(lastClose: CloseInfo): void {
    const attempt = this.wasStable() ? 0 : this.state.attempt;
    const delay = backoffDelayMs(attempt, BACKOFF_BASE_MS, BACKOFF_MAX_MS);
    this.setState({ status: 'waiting', attempt: attempt + 1, lastClose });
    this.timer = this.options.setTimer(() => this.connect(attempt + 1, lastClose), delay);
  }

  private setState(state: TransportState): void {
    this.state = state;
    this.options.onState(state);
  }
}
