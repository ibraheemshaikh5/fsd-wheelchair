// Drives the Transport over a simulated Wi-Fi uplink that is slower than the
// camera, to check that IMU messages (never dropped) do not sit behind a queue
// of frames for longer than the send budget, and that frames still flow.
import { describe, expect, it } from 'vitest';
import { SEND_BUDGET } from '../../capture/send-budget';
import { SOCKET_OPEN, type SocketLike, Transport } from '../../capture/transport';

const CAMERA_FPS = 60;
const IMU_FLUSH_MS = 50;
const SIMULATED_MS = 4000;
/** Let the transport measure the link before judging it. */
const WARM_UP_MS = 1000;
const IMU_PAYLOAD = { v: 1, type: 'imu', t: 0, samples: Array.from({ length: 3 }, () => [16, 0.1, 9.8, 0.2, 1, 2, 3]) };

interface Queued {
  readonly size: number;
  readonly sentAt: number;
  readonly json: boolean;
  remaining: number;
}

/**
 * A socket whose link sends `bytesPerMs`. Like Safari, bufferedAmount only drops
 * when a whole message has gone, which is the worst case for measuring the link.
 */
class SlowLink implements SocketLike {
  readyState = SOCKET_OPEN;
  binaryType: BinaryType = 'blob';
  onopen: (() => void) | null = null;
  onclose: ((event: { code: number; reason: string }) => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onerror: (() => void) | null = null;
  readonly jsonWaits: { sentAt: number; waitMs: number }[] = [];
  framesSent = 0;
  private queue: Queued[] = [];
  private nowMs = 0;

  constructor(private readonly bytesPerMs: number) {}

  get bufferedAmount(): number {
    return this.queue.reduce((sum, message) => sum + message.size, 0);
  }

  send(data: string | Uint8Array<ArrayBuffer>): void {
    const json = typeof data === 'string';
    const size = json ? data.length : data.byteLength;
    if (!json) this.framesSent += 1;
    this.queue.push({ size, sentAt: this.nowMs, json, remaining: size });
  }

  close(): void {}

  /** Run the link up to nowMs, one millisecond at a time. */
  advance(nowMs: number): void {
    for (; this.nowMs < nowMs; this.nowMs += 1) {
      let capacity = this.bytesPerMs;
      while (capacity > 0 && this.queue.length > 0) {
        const head = this.queue[0] as Queued;
        const sent = Math.min(capacity, head.remaining);
        head.remaining -= sent;
        capacity -= sent;
        if (head.remaining === 0) {
          this.queue.shift();
          if (head.json) this.jsonWaits.push({ sentAt: head.sentAt, waitMs: this.nowMs + 1 - head.sentAt });
        }
      }
    }
  }
}

const SMALLEST_FRAME_BYTES = 35_000;
const LARGEST_FRAME_BYTES = 50_000;
/** Time for the IMU message itself to go out once it reaches the front. */
const IMU_SEND_MS = 2;
/** A deterministic spread of 640 px JPEG sizes between 35 and 50 KB. */
const frameBytes = (k: number): number =>
  SMALLEST_FRAME_BYTES + ((k * 7919) % (LARGEST_FRAME_BYTES - SMALLEST_FRAME_BYTES));

function simulate(bytesPerMs: number): SlowLink {
  const link = new SlowLink(bytesPerMs);
  let now = 0;
  const transport = new Transport('wss://chair/ws/phone', {
    createSocket: () => link,
    setTimer: () => 0,
    clearTimer: () => {},
    onState: () => {},
    onMessage: () => {},
    sendBudget: SEND_BUDGET,
    now: () => now,
  });
  transport.start();
  link.onopen?.();
  let nextFrame = 0;
  for (now = 0; now < SIMULATED_MS; now += 1) {
    link.advance(now);
    if (now >= (nextFrame * 1000) / CAMERA_FPS) {
      transport.sendFrame(new Uint8Array(frameBytes(nextFrame)));
      nextFrame += 1;
    }
    if (now % IMU_FLUSH_MS === 0) transport.sendJson({ ...IMU_PAYLOAD, t: now });
  }
  link.advance(SIMULATED_MS + 1000);
  return link;
}

const framesPerSecond = (link: SlowLink): number => link.framesSent / (SIMULATED_MS / 1000);
const worstJsonWaitMs = (link: SlowLink): number =>
  Math.max(...link.jsonWaits.filter((w) => w.sentAt >= WARM_UP_MS).map((w) => w.waitMs));

describe('Transport on a link slower than the camera', () => {
  it.each([1000, 1500, 2000])('keeps IMU messages within the send budget at %d bytes/ms', (bytesPerMs) => {
    const link = simulate(bytesPerMs);
    expect(link.jsonWaits.length).toBeGreaterThan(0);
    expect(worstJsonWaitMs(link)).toBeLessThanOrEqual(SEND_BUDGET.budgetMs);
  });

  it('keeps IMU messages within one frame of the budget on a link slower than one frame per budget', () => {
    // 500 bytes/ms: a single 35-50 KB frame alone takes 70-100 ms to go.
    const slowestFrameMs = LARGEST_FRAME_BYTES / 500;
    expect(worstJsonWaitMs(simulate(500))).toBeLessThanOrEqual(slowestFrameMs + IMU_SEND_MS);
  });

  it.each([500, 1000, 1500, 2000])('still uses most of the link for frames at %d bytes/ms', (bytesPerMs) => {
    const averageFrame = (SMALLEST_FRAME_BYTES + LARGEST_FRAME_BYTES) / 2;
    const linkFps = bytesPerMs / (averageFrame / 1000);
    expect(framesPerSecond(simulate(bytesPerMs))).toBeGreaterThanOrEqual(0.8 * linkFps);
  });

  it('sends every camera frame when the link keeps up', () => {
    const link = simulate(4000);
    expect(framesPerSecond(link)).toBeGreaterThanOrEqual(CAMERA_FPS - 1);
    expect(worstJsonWaitMs(link)).toBeLessThanOrEqual(SEND_BUDGET.budgetMs);
  });
});
