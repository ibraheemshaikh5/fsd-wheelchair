import { describe, expect, it } from 'vitest';
import { ACK_TIMEOUT_MS } from '../../capture/frame-flow';
import { LinkControl } from '../../capture/link-control';
import {
  buildLinkMessage,
  bump,
  counterDelta,
  countSkip,
  linkSkips,
  linkView,
  ZERO_COUNTERS,
} from '../../capture/link-stats';

describe('link counters', () => {
  it('counts each skip reason in its own counter without changing the original', () => {
    const counters = countSkip(countSkip(countSkip(bump(ZERO_COUNTERS, 'captured'), 'flow'), 'encoder'), 'socket');
    expect(counters).toEqual({ ...ZERO_COUNTERS, captured: 1, skipped_flow: 1, skipped_encoder: 1, skipped_socket: 1 });
    expect(ZERO_COUNTERS.captured).toBe(0);
    // Only the link's own skips, not the encoder's, drive quality.
    expect(linkSkips(counters)).toBe(2);
    expect(counterDelta(counters, ZERO_COUNTERS)).toEqual(counters);
  });

  it('builds the link message with every contract field', () => {
    const counters = { captured: 60, sent: 12, acked: 11, skipped_flow: 40, skipped_encoder: 3, skipped_socket: 5 };
    const message = buildLinkMessage({ counters, inFlight: 2, jpegQuality: 0.5, ackRttMs: 312.345 }, 1_700_000);
    expect(message).toEqual({
      v: 1,
      type: 'link',
      t: 1_700_000,
      captured: 60,
      sent: 12,
      acked: 11,
      skipped_flow: 40,
      skipped_encoder: 3,
      skipped_socket: 5,
      in_flight: 2,
      jpeg_quality: 0.5,
      ack_rtt_ms: 312.3,
    });
    expect(buildLinkMessage({ counters, inFlight: 0, jpegQuality: 0.7, ackRttMs: null }, 1).ack_rtt_ms).toBeNull();
  });

  it('turns a window of counters into rates', () => {
    const window = { ...ZERO_COUNTERS, captured: 30, sent: 10, skipped_flow: 20 };
    const snapshot = { counters: { ...window, skipped_flow: 90 }, inFlight: 3, jpegQuality: 0.6, ackRttMs: 200 };
    expect(linkView(window, 500, snapshot, 'acking')).toEqual({
      capturedFps: 60,
      sentFps: 20,
      flowSkipsPerS: 40,
      flowSkips: 90,
      jpegQuality: 0.6,
      ackRttMs: 200,
      ackStatus: 'acking',
    });
    expect(linkView(window, 0, snapshot, 'none').sentFps).toBe(0);
  });
});

describe('LinkControl', () => {
  it('reports no link message until the service has acked a frame', () => {
    const link = new LinkControl(0.7, 0);
    link.captured();
    link.sent(0, 0);
    expect(link.tick(900, 5000).message).toBeNull();
    link.ack(0, 950);
    expect(link.tick(2000, 6000).message).toMatchObject({ type: 'link', t: 6000, sent: 1, acked: 1, ack_rtt_ms: 950 });
  });

  it('starts counters and flow control again on a new connection, keeping quality', () => {
    const link = new LinkControl(0.7, 0);
    link.ack(0, 0);
    for (let seq = 1; seq <= 10; seq += 1) {
      link.captured();
      if (seq <= 3) link.sent(seq, 0);
      else link.skipped('flow');
    }
    expect(link.maySend(10)).toBe(false);
    const before = link.tick(1000, 1000);
    expect(before.view.jpegQuality).toBe(0.6);
    const epoch = link.connection;
    link.reset(1000);
    expect(link.connection).not.toBe(epoch);
    expect(link.maySend(1000)).toBe(true);
    const after = link.tick(2000, 2000);
    // The service acked on the last connection, so it knows the link type: report from the start.
    expect(after.message).toMatchObject({ type: 'link', captured: 0, sent: 0, acked: 0, ack_rtt_ms: null });
    expect(after.view).toMatchObject({ capturedFps: 0, flowSkips: 0, jpegQuality: 0.6, ackStatus: 'waiting' });
    expect(link.jpegQuality).toBe(0.6);
  });

  it('counts each sent frame acked at most once, so acked never exceeds sent', () => {
    const link = new LinkControl(0.7, 0);
    link.sent(0, 0);
    link.sent(1, 10);
    link.ack(7, 20);
    link.ack(0, 30);
    link.ack(0, 31);
    link.ack(1, 40);
    link.ack(0, 41);
    expect(link.tick(1000, 1000).message).toMatchObject({ sent: 2, acked: 2, ack_rtt_ms: 30 });
  });

  it('reports a slow round trip even when every ack takes longer than the ack timeout', () => {
    const link = new LinkControl(0.7, 0);
    link.sent(0, 0);
    link.sent(1, 100);
    link.ack(0, 1500);
    expect(link.tick(1600, 1600).message).toMatchObject({ acked: 1, ack_rtt_ms: 1500 });
    expect(link.tick(2600, 2600).view.ackRttMs).toBe(1500);
  });

  it('waits for the first ack before calling the service one that never acks', () => {
    const link = new LinkControl(0.7, 0);
    // No frame sent yet (camera muted): nothing to judge by.
    expect(link.tick(5000, 5000).view.ackStatus).toBe('waiting');
    link.sent(0, 5000);
    expect(link.tick(5000 + ACK_TIMEOUT_MS, 6000).view.ackStatus).toBe('waiting');
    expect(link.tick(5001 + ACK_TIMEOUT_MS, 6001).view.ackStatus).toBe('none');
    link.ack(0, 6100);
    expect(link.tick(6200, 6200).view.ackStatus).toBe('acking');
    // After a reconnect the service is known to ack: a slow first ack is just waited for.
    link.reset(7000);
    link.sent(1, 7000);
    expect(link.tick(9000, 9000).view.ackStatus).toBe('waiting');
  });
});
