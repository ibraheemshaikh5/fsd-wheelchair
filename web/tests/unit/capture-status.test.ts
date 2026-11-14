import { describe, expect, it } from 'vitest';
import type { LinkView } from '../../capture/link-stats';
import { INITIAL_LIVE, type LiveState, captureStatusRows, linkStatusRows, stoppedLive } from '../../capture/live';
import { describeConnection, describeStartError } from '../../capture/status';
import { CLOSE_BUSY, CLOSE_IDLE, CLOSE_NO_REPLY, CLOSE_REPLACED, NO_REPLY_REASON } from '../../capture/transport';

describe('describeConnection', () => {
  it('describes each state in plain words', () => {
    expect(describeConnection({ status: 'open', attempt: 0, lastClose: null })).toEqual({
      text: 'Connected',
      level: 'ok',
    });
    expect(describeConnection({ status: 'connecting', attempt: 0, lastClose: null }).text).toBe('Connecting…');
    expect(describeConnection({ status: 'stopped', attempt: 0, lastClose: null })).toEqual({
      text: 'Stopped',
      level: 'none',
    });
  });

  it('explains why the service closed the connection', () => {
    const refused = describeConnection({ status: 'waiting', attempt: 1, lastClose: { code: 1008, reason: '' } });
    expect(refused.text).toContain('refused');
    expect(refused.level).toBe('bad');

    const lost = describeConnection({ status: 'waiting', attempt: 3, lastClose: { code: 1006, reason: '' } });
    expect(lost.text).toBe('Connection lost, retrying (attempt 3)');
    expect(lost.level).toBe('warn');
  });

  it('says another phone or tab took over, without retrying', () => {
    const taken = describeConnection({
      status: 'stopped',
      attempt: 0,
      lastClose: { code: CLOSE_REPLACED, reason: 'replaced by a newer phone connection' },
    });
    expect(taken.text).toContain('took over');
    expect(taken.text).toContain('Tap Start');
    expect(taken.text).not.toContain('retrying');
    expect(taken.level).toBe('bad');
  });

  it('says another phone has priority, without retrying', () => {
    const busy = describeConnection({
      status: 'stopped',
      attempt: 0,
      lastClose: { code: CLOSE_BUSY, reason: 'another phone has priority' },
    });
    expect(busy.text).toContain('priority');
    expect(busy.text).not.toContain('retrying');
    expect(busy.level).toBe('bad');
  });

  it('explains an idle timeout from the service and keeps retrying', () => {
    const idle = describeConnection({ status: 'waiting', attempt: 2, lastClose: { code: CLOSE_IDLE, reason: '' } });
    expect(idle.text).toContain('no data');
    expect(idle.text).toContain('attempt 2');
    expect(idle.level).toBe('warn');
  });

  it('explains a link that stopped answering', () => {
    const silent = describeConnection({
      status: 'waiting',
      attempt: 1,
      lastClose: { code: CLOSE_NO_REPLY, reason: NO_REPLY_REASON },
    });
    expect(silent.text).toContain('No reply from the service');
    expect(silent.text).toContain('attempt 1');
    expect(silent.level).toBe('warn');
  });

  it('describes a plain stop after an old drop as just stopped', () => {
    expect(describeConnection({ status: 'stopped', attempt: 0, lastClose: { code: 1006, reason: '' } }).text).toBe(
      'Stopped',
    );
  });
});

describe('describeStartError', () => {
  it('turns browser camera errors into instructions', () => {
    expect(describeStartError(new DOMException('x', 'NotAllowedError'))).toContain('Camera permission denied');
    expect(describeStartError(new DOMException('x', 'NotFoundError'))).toBe('No usable rear camera was found.');
    expect(describeStartError(new DOMException('x', 'OverconstrainedError'))).toBe('No usable rear camera was found.');
    expect(describeStartError(new Error('boom'))).toBe('boom');
    expect(describeStartError('weird')).toBe('Unexpected error starting the camera.');
  });
});

describe('captureStatusRows', () => {
  const valueOf = (live: LiveState, label: string): string | undefined =>
    captureStatusRows(live).find((row) => row.label === label)?.value;

  const busy: LiveState = {
    connection: { status: 'open', attempt: 0, lastClose: null },
    counters: { captured: 30, sent: 25, skipped: 5 },
    cameraLabel: 'Back Ultra Wide Camera',
    gpsAccuracyM: 4.24,
    headingDeg: 89.6,
    motion: 'denied',
    wakeLock: false,
    clock: { rtt_ms: 41.4, offset_ms: 3 },
    link: null,
  };

  it('shows every live reading', () => {
    expect(captureStatusRows(busy)).toEqual([
      { label: 'Service', value: 'Connected', level: 'ok' },
      { label: 'Camera', value: 'Back Ultra Wide Camera', level: 'ok' },
      { label: 'Frames', value: '25 sent · 5 skipped · 30 captured', level: 'none' },
      { label: 'GPS', value: '±4.2 m', level: 'ok' },
      { label: 'Compass', value: '90°', level: 'none' },
      { label: 'Motion', value: 'Denied: no compass or IMU', level: 'bad' },
      { label: 'Screen lock', value: 'Not held: set Auto-Lock to Never', level: 'warn' },
      { label: 'Round trip', value: '41 ms', level: 'none' },
      { label: 'Send rate', value: '—', level: 'none' },
      { label: 'Held back', value: '—', level: 'none' },
      { label: 'JPEG quality', value: '—', level: 'none' },
      { label: 'Frame ack', value: '—', level: 'none' },
    ]);
    expect(valueOf({ ...busy, motion: 'granted', wakeLock: true }, 'Motion')).toBe('Allowed');
    expect(valueOf({ ...busy, motion: 'not-needed' }, 'Motion')).toBe('Available');
    expect(valueOf({ ...busy, wakeLock: true }, 'Screen lock')).toBe('Kept on');
  });

  it('shows nothing live after a stop, keeping only why the link closed', () => {
    const replaced = {
      ...busy,
      connection: { status: 'stopped' as const, attempt: 0, lastClose: { code: CLOSE_REPLACED, reason: '' } },
    };
    const after = stoppedLive(replaced);
    expect(after).toEqual({ ...INITIAL_LIVE, connection: replaced.connection });
    expect(valueOf(after, 'GPS')).toBe('Waiting for fix');
    expect(valueOf(after, 'Camera')).toBe('—');
    expect(valueOf(after, 'Compass')).toBe('—');
    expect(valueOf(after, 'Round trip')).toBe('—');
    expect(valueOf(after, 'Service')).toContain('took over');
    expect(stoppedLive(busy).connection).toEqual({ status: 'stopped', attempt: 0, lastClose: null });
  });
});

describe('linkStatusRows', () => {
  const healthy: LinkView = {
    capturedFps: 59.6,
    sentFps: 59.6,
    flowSkipsPerS: 0,
    flowSkips: 3,
    jpegQuality: 0.7,
    ackRttMs: 36.4,
    ackStatus: 'acking',
  };

  it('shows a healthy link without warnings', () => {
    expect(linkStatusRows(healthy)).toEqual([
      { label: 'Send rate', value: '60 of 60 fps', level: 'ok' },
      { label: 'Held back', value: '0/s by slow link (3 total)', level: 'none' },
      { label: 'JPEG quality', value: '0.70', level: 'none' },
      { label: 'Frame ack', value: '36 ms', level: 'none' },
    ]);
  });

  it('warns about a weak link: few frames sent, many held back, slow acks', () => {
    const weak = { ...healthy, sentFps: 9.8, flowSkipsPerS: 48, flowSkips: 500, jpegQuality: 0.4, ackRttMs: 310 };
    expect(linkStatusRows(weak)).toEqual([
      { label: 'Send rate', value: '10 of 60 fps', level: 'warn' },
      { label: 'Held back', value: '48/s by slow link (500 total)', level: 'warn' },
      { label: 'JPEG quality', value: '0.40', level: 'none' },
      { label: 'Frame ack', value: '310 ms', level: 'warn' },
    ]);
  });

  it('says when the service does not ack frames, or no round trip is known yet', () => {
    const value = (link: LinkView): string | undefined => linkStatusRows(link).find((r) => r.label === 'Frame ack')?.value;
    expect(value({ ...healthy, ackStatus: 'none', ackRttMs: null })).toBe('No acks (older service)');
    expect(value({ ...healthy, ackRttMs: null })).toBe('—');
  });

  it('does not blame the service while the first ack on a connection is still coming', () => {
    const waiting = linkStatusRows({ ...healthy, ackStatus: 'waiting', ackRttMs: null }).find((r) => r.label === 'Frame ack');
    expect(waiting).toEqual({ label: 'Frame ack', value: 'Waiting for first ack', level: 'none' });
  });

  it('is part of the capture status panel', () => {
    const rows = captureStatusRows({ ...INITIAL_LIVE, link: healthy });
    expect(rows.find((row) => row.label === 'Send rate')?.value).toBe('60 of 60 fps');
  });
});
