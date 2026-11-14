// Frame flow control, adaptive quality and link reports, driven through the
// Session over simulated links. Wall and monotonic clocks move together here.
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { FrameHeader, LinkMessage } from '../../shared/protocol';
import { ACK_TIMEOUT_MS, MAX_FRAMES_IN_FLIGHT } from '../../capture/frame-flow';
import { DEFAULT_SETTINGS } from '../../capture/settings';
import { type Deferred, deferred, flush, harness, OPEN, startFully, type Harness } from './session-harness';

const WALL_BASE = 1_000_000;
const MONO_BASE = 5_000;
/** The simulated camera's rate unless a test sets one: 60 fps, the training-data setting and the busiest link. */
const CAMERA_FPS = 60;
const FRAME_MS = 1000 / CAMERA_FPS;
/** The phone's default when driving. */
const DRIVING_FPS = DEFAULT_SETTINGS.fps;

function header(bytes: Uint8Array<ArrayBuffer>): FrameHeader {
  const length = new DataView(bytes.buffer).getUint32(0, true);
  return JSON.parse(new TextDecoder().decode(bytes.subarray(4, 4 + length))) as FrameHeader;
}

function setTime(h: Harness, ms: number): void {
  h.setNow(WALL_BASE + ms);
  h.setMonotonic(MONO_BASE + ms);
}

const ack = (h: Harness, seq: number): void =>
  h.transport().handlers.onMessage({ v: 1, type: 'ack', seq, t: WALL_BASE });

const linkMessages = (h: Harness): LinkMessage[] =>
  h
    .transport()
    .sendJson.mock.calls.map(([message]) => message as LinkMessage)
    .filter((message) => message.type === 'link');

/** When the service's ack for a frame sent at `sentMs` reaches the phone. */
type LinkModel = (sentMs: number) => number;

/** Fixed latency, unlimited bandwidth: every ack comes back `delayMs` after the send. */
const delayedAcks =
  (delayMs: number): LinkModel =>
  (sentMs) =>
    sentMs + delayMs;

/** One frame on the wire at a time, `transmitMs` each, then `rttMs` until its ack is back. */
function serialLink(transmitMs: number, rttMs: number): LinkModel {
  let busyUntil = 0;
  return (sentMs) => {
    busyUntil = Math.max(sentMs, busyUntil) + transmitMs;
    return busyUntil + rttMs;
  };
}

interface SimResult {
  readonly sent: number;
  /** Frames sent after the first ack. */
  readonly gatedSent: number;
  /**
   * Most frames awaiting their ack when one was sent, counting only sends after
   * the first ack: until then the page cannot tell an acking service from an
   * older one, and only the send budget applies (by contract).
   */
  readonly maxInFlight: number;
  /** Capture-to-ack age of every frame sent after the first ack. */
  readonly ages: readonly number[];
}

interface SimOptions {
  /** Simulated time to start at, so a second phase can follow the first. */
  readonly startMs?: number;
  /** How long each JPEG encode takes; 0 resolves at once. */
  readonly encodeMs?: number;
  /** Frames the camera delivers per second (CAMERA_FPS unless set). */
  readonly fps?: number;
}

type Encoded = { bytes: Uint8Array<ArrayBuffer>; w: number; h: number; rot: 0 } | null;

/** Capture at `fps` (default 60) for `durationMs`, acking each sent frame when the link model says. */
async function simulate(h: Harness, link: LinkModel, durationMs: number, options: SimOptions = {}): Promise<SimResult> {
  const { startMs = 0, encodeMs = 0, fps = CAMERA_FPS } = options;
  const frameMs = 1000 / fps;
  const pending: { seq: number; t: number; ackAt: number; gated: boolean }[] = [];
  const encoding: { readyAt: number; done: Deferred<Encoded> }[] = [];
  const ages: number[] = [];
  let sent = 0;
  let gatedSent = 0;
  let acked = 0;
  let maxInFlight = 0;
  let now = startMs;
  h.transport().sendFrame.mockImplementation((bytes) => {
    const { seq, t } = header(bytes);
    const gated = acked > 0;
    pending.push({ seq, t, ackAt: link(now), gated });
    sent += 1;
    if (gated) {
      gatedSent += 1;
      maxInFlight = Math.max(maxInFlight, sent - acked);
    }
    return true;
  });
  if (encodeMs > 0) {
    vi.mocked(h.deps.encodeFrame).mockImplementation(() => {
      const done = deferred<Encoded>();
      encoding.push({ readyAt: now + encodeMs, done });
      return done.promise;
    });
  }
  let nextFrame = Math.ceil(startMs / frameMs);
  for (now = startMs; now <= startMs + durationMs; now += 1) {
    setTime(h, now);
    while (pending.length > 0 && (pending[0]?.ackAt ?? Infinity) <= now) {
      const frame = pending.shift() as { seq: number; t: number; gated: boolean };
      acked += 1;
      if (frame.gated) ages.push(WALL_BASE + now - frame.t);
      ack(h, frame.seq);
    }
    while (encoding.length > 0 && (encoding[0]?.readyAt ?? Infinity) <= now) {
      encoding.shift()?.done.resolve({ bytes: new Uint8Array([1, 2]), w: 2, h: 1, rot: 0 });
      await flush();
    }
    if (now > 0 && now % 1000 === 0) h.sensors.tick?.();
    if (now >= nextFrame * frameMs) {
      nextFrame += 1;
      h.frame.onFrame?.(undefined);
      await flush();
    }
  }
  return { sent, gatedSent, maxInFlight, ages };
}

const lastLink = (h: Harness): LinkMessage => linkMessages(h).at(-1) as LinkMessage;

describe('Session flow control', () => {
  let h: Harness;
  beforeEach(async () => {
    h = harness();
    await startFully(h);
    setTime(h, 0);
    h.transport().state(OPEN);
  });

  it('on a link whose acks come 300 ms late, keeps 2 frames in flight and none old', async () => {
    const delayMs = 300;
    const result = await simulate(h, delayedAcks(delayMs), 4000);
    expect(result.ages.length).toBeGreaterThan(20);
    expect(result.maxInFlight).toBe(MAX_FRAMES_IN_FLIGHT);
    expect(Math.max(...result.ages)).toBeLessThanOrEqual(MAX_FRAMES_IN_FLIGHT * FRAME_MS + delayMs);
    // 2 frames per round trip: about 6.7 fps get through (after the first ack, 300 ms in), not 60.
    const gatedFps = result.gatedSent / 3.7;
    expect(gatedFps).toBeGreaterThanOrEqual(6);
    expect(gatedFps).toBeLessThanOrEqual(7.5);
  });

  it('on a link that carries 10 frames a second, uses it fully but never lets a frame age past 2 on the wire', async () => {
    const transmitMs = 100;
    const rttMs = 40;
    const result = await simulate(h, serialLink(transmitMs, rttMs), 4000);
    expect(result.ages.length).toBeGreaterThan(20);
    expect(result.maxInFlight).toBe(MAX_FRAMES_IN_FLIGHT);
    expect(Math.max(...result.ages)).toBeLessThanOrEqual(MAX_FRAMES_IN_FLIGHT * transmitMs + rttMs + FRAME_MS);
    expect(result.sent / 4).toBeGreaterThanOrEqual(9);
  });

  it('lowers JPEG quality on a slow link and reports it once a second', async () => {
    await simulate(h, delayedAcks(300), 3500);
    const qualities = vi.mocked(h.deps.encodeFrame).mock.calls.map(([, quality]) => quality);
    expect(qualities[0]).toBe(0.7);
    expect(qualities.at(-1)).toBe(0.4);
    const messages = linkMessages(h);
    expect(messages).toHaveLength(3);
    const last = messages.at(-1) as LinkMessage;
    expect(last).toMatchObject({ v: 1, type: 'link', t: WALL_BASE + 3000, jpeg_quality: 0.4, skipped_socket: 0 });
    expect(last.in_flight).toBeLessThanOrEqual(MAX_FRAMES_IN_FLIGHT);
    expect(last.skipped_flow).toBeGreaterThan(100);
    expect(last.captured).toBe(last.sent + last.skipped_flow + last.skipped_encoder + last.skipped_socket);
    expect(last.ack_rtt_ms).toBeCloseTo(300, 0);
    // Never droppable: the service needs to see a weak link.
    const call = h.transport().sendJson.mock.calls.find(([message]) => (message as LinkMessage).type === 'link');
    expect(call?.[1]).toBe(false);
    expect(h.session.live.link).toMatchObject({ ackStatus: 'acking', jpegQuality: 0.4 });
    expect(h.session.live.link?.sentFps).toBeLessThan(15);
    expect(h.session.live.link?.capturedFps).toBeGreaterThan(55);
  });

  it('frees the slot of a frame whose ack was lost after one second', async () => {
    h.frame.onFrame?.(undefined);
    await flush();
    ack(h, 0);
    setTime(h, 100);
    for (let i = 0; i < 4; i += 1) {
      h.frame.onFrame?.(undefined);
      await flush();
    }
    expect(h.transport().sendFrame).toHaveBeenCalledTimes(1 + MAX_FRAMES_IN_FLIGHT);
    setTime(h, 100 + ACK_TIMEOUT_MS - 1);
    h.frame.onFrame?.(undefined);
    await flush();
    expect(h.transport().sendFrame).toHaveBeenCalledTimes(1 + MAX_FRAMES_IN_FLIGHT);
    setTime(h, 100 + ACK_TIMEOUT_MS);
    h.frame.onFrame?.(undefined);
    await flush();
    expect(h.transport().sendFrame).toHaveBeenCalledTimes(2 + MAX_FRAMES_IN_FLIGHT);
    h.sensors.tick?.();
    const sent = 2 + MAX_FRAMES_IN_FLIGHT;
    expect(linkMessages(h).at(-1)).toMatchObject({ captured: 7, sent, acked: 1, skipped_flow: 7 - sent });
  });

  it('with a service that never acks, streams every frame under the send budget alone', async () => {
    const result = await simulate(h, () => Infinity, 1000);
    expect(result.sent).toBeGreaterThanOrEqual(CAMERA_FPS);
    expect(h.deps.encodeFrame).toHaveBeenCalledTimes(result.sent);
    // The socket's budget still applies: refused frames are counted as such.
    h.transport().sendFrame.mockReturnValue(false);
    for (let i = 0; i < 40; i += 1) {
      h.frame.onFrame?.(undefined);
      await flush();
    }
    setTime(h, 2000);
    h.sensors.tick?.();
    // An older service may not know the link type, so none is sent until it acks.
    expect(linkMessages(h)).toEqual([]);
    expect(h.session.live.link).toMatchObject({ ackStatus: 'none', jpegQuality: 0.6 });
    expect(vi.mocked(h.deps.encodeFrame).mock.calls.every(([, quality]) => quality === 0.7)).toBe(true);
  });

  it('starts flow control again on reconnect, until the new connection acks', async () => {
    h.frame.onFrame?.(undefined);
    await flush();
    ack(h, 0);
    for (let i = 0; i < 5; i += 1) {
      h.frame.onFrame?.(undefined);
      await flush();
    }
    expect(h.transport().sendFrame).toHaveBeenCalledTimes(1 + MAX_FRAMES_IN_FLIGHT);
    h.transport().state({ status: 'waiting', attempt: 1, lastClose: { code: 1006, reason: '' } });
    expect(h.session.live.link).toBeNull();
    h.transport().state(OPEN);
    h.transport().sendFrame.mockClear();
    for (let i = 0; i < 5; i += 1) {
      h.frame.onFrame?.(undefined);
      await flush();
    }
    expect(h.transport().sendFrame).toHaveBeenCalledTimes(5);
  });

  it('drops a frame that finishes encoding after its connection was replaced', async () => {
    const encoding = deferred<Encoded>();
    vi.mocked(h.deps.encodeFrame).mockReturnValueOnce(encoding.promise);
    h.frame.onFrame?.(undefined);
    h.transport().state({ status: 'waiting', attempt: 1, lastClose: { code: 1006, reason: '' } });
    h.transport().state(OPEN);
    encoding.resolve({ bytes: new Uint8Array([1]), w: 1, h: 1, rot: 0 });
    await flush();
    expect(h.transport().sendFrame).not.toHaveBeenCalled();
    expect(h.session.live.counters).toEqual({ captured: 1, sent: 0, skipped: 1 });
    ack(h, 99);
    h.sensors.tick?.();
    // The new connection's counters do not include the old connection's frame,
    // and an ack for a frame this connection never sent is not counted.
    expect(linkMessages(h).at(-1)).toMatchObject({ captured: 0, sent: 0, skipped_encoder: 0, acked: 0 });
  });

  it('counts frames the encoder could not take as encoder skips', async () => {
    const slow = deferred<Encoded>();
    h.frame.onFrame?.(undefined);
    await flush();
    ack(h, 0);
    vi.mocked(h.deps.encodeFrame).mockReturnValueOnce(slow.promise).mockReturnValueOnce(slow.promise);
    h.frame.onFrame?.(undefined);
    h.frame.onFrame?.(undefined);
    // Two encoding and none in flight: the ack window has room, but the encoder is full.
    h.frame.onFrame?.(undefined);
    expect(h.deps.encodeFrame).toHaveBeenCalledTimes(3);
    slow.resolve(null);
    await flush();
    h.sensors.tick?.();
    expect(linkMessages(h).at(-1)).toMatchObject({ captured: 4, sent: 1, skipped_encoder: 3, skipped_flow: 0 });
  });

  it('on a healthy link with a slow encoder, sends every frame: encoding frames hold no ack slot', async () => {
    // At the driving rate, 25 ms to encode plus 40 ms to the ack is 65 ms per
    // frame, more than one frame interval, but only 1 frame is ever waiting for its ack.
    await simulate(h, delayedAcks(40), 4000, { encodeMs: 25, fps: DRIVING_FPS });
    const last = lastLink(h);
    expect(last).toMatchObject({ skipped_flow: 0, skipped_encoder: 0, skipped_socket: 0, jpeg_quality: 0.7 });
    expect(h.session.live.link?.sentFps).toBeGreaterThanOrEqual(DRIVING_FPS - 1);
  });

  it('brings JPEG quality back to the setting once a weak link recovers', async () => {
    await simulate(h, delayedAcks(300), 3500);
    expect(lastLink(h).jpeg_quality).toBe(0.4);
    await simulate(h, delayedAcks(40), 8000, { startMs: 3501, encodeMs: 25 });
    expect(lastLink(h).jpeg_quality).toBe(0.7);
    expect(vi.mocked(h.deps.encodeFrame).mock.calls.at(-1)?.[1]).toBe(0.7);
  });

  it('reports the round trip when every ack takes longer than the ack timeout', async () => {
    await simulate(h, delayedAcks(1500), 3500);
    expect(lastLink(h).ack_rtt_ms).toBeCloseTo(1500, 0);
    expect(h.session.live.link?.ackRttMs).toBeCloseTo(1500, 0);
  });

  it('after a reconnect, reports the link from the first second without waiting for an ack', async () => {
    h.frame.onFrame?.(undefined);
    await flush();
    ack(h, 0);
    h.transport().state({ status: 'waiting', attempt: 1, lastClose: { code: 1006, reason: '' } });
    h.transport().state(OPEN);
    h.transport().sendJson.mockClear();
    setTime(h, 1000);
    h.sensors.tick?.();
    expect(linkMessages(h)).toEqual([expect.objectContaining({ type: 'link', captured: 0, sent: 0, acked: 0 })]);
    expect(h.session.live.link).toMatchObject({ ackStatus: 'waiting' });
  });

  // At the driving rate a frame leaves every 50 ms, so a 60 ms round trip never
  // fills the 2-frame window: every frame goes, at the configured quality.
  it('at the driving rate, sends every frame at full quality on a 60 ms link', async () => {
    await simulate(h, delayedAcks(60), 6000, { encodeMs: 15, fps: DRIVING_FPS });
    expect(lastLink(h)).toMatchObject({ skipped_flow: 0, skipped_encoder: 0, skipped_socket: 0 });
    expect(h.session.live.link?.sentFps).toBeGreaterThanOrEqual(DRIVING_FPS - 1);
    expect(lastLink(h).ack_rtt_ms).toBeCloseTo(60, 0);
    expect(lastLink(h).jpeg_quality).toBe(DEFAULT_SETTINGS.jpegQuality);
  });

  it('at the driving rate, lowers quality on a link too slow to carry 20 fps', async () => {
    // Two frames per 140 ms round trip is about 14 fps: under the model's 20.
    await simulate(h, delayedAcks(140), 6000, { fps: DRIVING_FPS });
    expect(h.session.live.link?.sentFps).toBeLessThan(DRIVING_FPS * 0.9);
    expect(lastLink(h).jpeg_quality).toBeLessThan(DEFAULT_SETTINGS.jpegQuality);
  });

  it('lowers quality while acks take longer than 150 ms', async () => {
    await simulate(h, delayedAcks(300), 6000, { encodeMs: 15 });
    expect(lastLink(h).jpeg_quality).toBeLessThan(DEFAULT_SETTINGS.jpegQuality);
  });
});
