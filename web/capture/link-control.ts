// The frame stream's view of the link for one session: ack-based flow control,
// per-connection counters and adaptive JPEG quality, behind the few calls the
// Session makes. The decisions themselves live in the pure modules it uses.

import type { LinkMessage } from '../shared/protocol';
import {
  expireFrames,
  type FrameFlow,
  INITIAL_FLOW,
  isNewAck,
  maySend,
  recordAck,
  recordSent,
} from './frame-flow';
import {
  bump,
  counterDelta,
  countSkip,
  type LinkCounters,
  type LinkSnapshot,
  type LinkView,
  buildLinkMessage,
  linkView,
  type SkipReason,
  ZERO_COUNTERS,
} from './link-stats';
import { adjustQuality, initialQuality, type QualityState } from './quality';

export interface LinkTick {
  /** The `link` message to send. */
  readonly message: LinkMessage;
  readonly view: LinkView;
}

export class LinkControl {
  private flow: FrameFlow = INITIAL_FLOW;
  private counters: LinkCounters = ZERO_COUNTERS;
  private windowStart: LinkCounters = ZERO_COUNTERS;
  private windowAtMs: number;
  private quality: QualityState;
  private epoch = 0;

  /** Times are on the monotonic clock. */
  constructor(configuredQuality: number, nowMs: number) {
    this.quality = initialQuality(configuredQuality);
    this.windowAtMs = nowMs;
  }

  /** JPEG quality for the next frame. */
  get jpegQuality(): number {
    return this.quality.quality;
  }

  /** Changes on every reset, so a frame begun on an older connection can be told apart. */
  get connection(): number {
    return this.epoch;
  }

  /** A new connection: flow control and counters start again; quality carries over. */
  reset(nowMs: number): void {
    this.epoch += 1;
    this.flow = INITIAL_FLOW;
    this.counters = ZERO_COUNTERS;
    this.windowStart = ZERO_COUNTERS;
    this.windowAtMs = nowMs;
  }

  captured(): void {
    this.counters = bump(this.counters, 'captured');
  }

  skipped(reason: SkipReason): void {
    this.counters = countSkip(this.counters, reason);
  }

  /** Whether a frame may go out now (checked before encoding it, and again before sending it). */
  maySend(nowMs: number): boolean {
    return maySend(this.flow, nowMs);
  }

  sent(seq: number, nowMs: number): void {
    this.flow = recordSent(this.flow, seq, nowMs);
    this.counters = bump(this.counters, 'sent');
  }

  /** Counted only for the first ack of a frame this connection sent, so acked never exceeds sent. */
  ack(seq: number, nowMs: number): void {
    if (isNewAck(this.flow, seq)) this.counters = bump(this.counters, 'acked');
    this.flow = recordAck(this.flow, seq, nowMs);
  }

  /** Once a second: adapt quality to the last window, and report. */
  tick(nowMs: number, wallNowMs: number): LinkTick {
    const window = counterDelta(this.counters, this.windowStart);
    this.quality = adjustQuality(this.quality, {
      seconds: (nowMs - this.windowAtMs) / 1000,
      captured: window.captured,
      sent: window.sent,
      ackRttMs: this.flow.rttMs,
    });
    this.flow = expireFrames(this.flow, nowMs);
    const snapshot: LinkSnapshot = {
      counters: this.counters,
      inFlight: this.flow.inFlight.length,
      jpegQuality: this.quality.quality,
      ackRttMs: this.flow.rttMs,
    };
    const view = linkView(window, nowMs - this.windowAtMs, snapshot);
    this.windowStart = this.counters;
    this.windowAtMs = nowMs;
    return { message: buildLinkMessage(snapshot, wallNowMs), view };
  }
}
