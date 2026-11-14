import { describe, expect, it } from 'vitest';
import {
  formatAge,
  formatBearing,
  formatDeclination,
  frameBadge,
  recordButtonState,
  statusRows,
} from '../../viewer/format';
import { parseStatus, type RecordingStatus, type ServiceStatus } from '../../shared/status';

describe('formatBearing', () => {
  it('rounds to whole degrees and wraps 360 to 0', () => {
    expect(formatBearing(359.6)).toBe('0°');
    expect(formatBearing(359.4)).toBe('359°');
    expect(formatBearing(0)).toBe('0°');
    expect(formatBearing(90.5)).toBe('91°');
  });
});

describe('formatAge', () => {
  it('formats milliseconds and seconds', () => {
    expect(formatAge(null)).toBe('—');
    expect(formatAge(42.4)).toBe('42 ms');
    expect(formatAge(2500)).toBe('2.5 s');
  });
});

const IDLE_RECORDING: RecordingStatus = {
  active: false,
  finishing: false,
  session: null,
  frames: 0,
  sensors: 0,
  dropped: 0,
  error: null,
  end_reason: null,
};

const BASE: ServiceStatus = {
  type: 'status',
  phone_connected: true,
  clock_synced: true,
  sensors_ok: true,
  frame: { seq: 10, w: 640, h: 480, age_ms: 40, fresh: true },
  gps: {
    lat: 38.98691,
    lon: -76.94262,
    accuracy_m: 4.2,
    altitude_m: 40,
    speed_mps: 1.25,
    course_deg: 90,
    age_ms: 300,
    fresh: true,
  },
  heading: {
    deg: 90,
    source: 'gps_course',
    reference: 'true',
    declination_deg: -11,
    compass_deg: 95,
    compass_accuracy_deg: 10,
    compass_age_ms: 50,
    compass_fresh: true,
  },
  clock: { offset_ms: -3, rtt_ms: 45, samples: 5 },
  stats: {
    fps: 9.96,
    latency_p50_ms: 80,
    latency_p95_ms: 210,
    frames: 500,
    missed: 3,
    rejected: 0,
    rate_limited: 0,
  },
  camera: { label: 'Back Ultra Wide Camera', width: 640, height: 480, fps: 10 },
  mount: { height_m: 1.1, pitch_deg: -10 },
  recording: IDLE_RECORDING,
};

const row = (status: ServiceStatus, label: string) => statusRows(status).find((r) => r.label === label);

describe('statusRows', () => {
  it('summarises a healthy stream', () => {
    expect(row(BASE, 'Phone')).toEqual({ label: 'Phone', value: 'Connected, sensors OK', level: 'ok' });
    expect(row(BASE, 'Camera')?.value).toBe('Back Ultra Wide Camera · 640×480 · 10.0 fps · age 40 ms');
    expect(row(BASE, 'GPS')?.value).toBe('38.986910, -76.942620 · ±4.2 m · 1.3 m/s · age 300 ms');
    expect(row(BASE, 'Heading')?.value).toBe('90° true from GPS course (compass 95° ±10°) · declination 11.0° W');
    expect(row(BASE, 'Latency')?.value).toBe('p50 80 ms · p95 210 ms · RTT 45 ms');
    expect(row(BASE, 'Frames')?.value).toBe('500 received · 3 missed · 0 rejected · 0 rate-limited');
    expect(row(BASE, 'Recording')).toEqual({ label: 'Recording', value: 'Off', level: 'none' });
  });

  it('flags stale and missing readings', () => {
    const stale: ServiceStatus = {
      ...BASE,
      sensors_ok: false,
      frame: { ...BASE.frame!, fresh: false, age_ms: 4000 },
      gps: null,
      heading: null,
    };
    expect(row(stale, 'Phone')).toEqual({ label: 'Phone', value: 'Connected, sensors stale', level: 'warn' });
    expect(row(stale, 'Camera')?.level).toBe('bad');
    expect(row(stale, 'GPS')).toEqual({ label: 'GPS', value: 'No fix yet', level: 'bad' });
    expect(row(stale, 'Heading')).toEqual({ label: 'Heading', value: 'None', level: 'warn' });
  });

  it('reports a disconnected phone', () => {
    const offline: ServiceStatus = { ...BASE, phone_connected: false, sensors_ok: false };
    expect(row(offline, 'Phone')).toEqual({ label: 'Phone', value: 'Not connected', level: 'bad' });
  });

  it('marks poor latency and unknown values', () => {
    const slow: ServiceStatus = {
      ...BASE,
      clock: null,
      stats: { ...BASE.stats, latency_p50_ms: null, latency_p95_ms: null },
    };
    expect(row(slow, 'Latency')).toEqual({ label: 'Latency', value: 'Waiting for clock sync', level: 'warn' });

    const laggy: ServiceStatus = { ...BASE, stats: { ...BASE.stats, latency_p95_ms: 900 } };
    expect(row(laggy, 'Latency')?.level).toBe('bad');
  });

  it('shows compass-only heading and an uncalibrated compass', () => {
    const compass: ServiceStatus = {
      ...BASE,
      heading: { ...BASE.heading!, deg: 84, source: 'compass', compass_accuracy_deg: null },
    };
    expect(row(compass, 'Heading')?.value).toBe(
      '84° true from compass (compass 95°, uncalibrated) · declination 11.0° W',
    );

    const staleCompass: ServiceStatus = {
      ...BASE,
      heading: { ...BASE.heading!, deg: null, source: null, compass_fresh: false },
    };
    expect(row(staleCompass, 'Heading')).toEqual({
      label: 'Heading',
      value: 'None (compass 95° ±10°, stale)',
      level: 'warn',
    });
  });

  it('shows an active recording and its errors', () => {
    const recording: ServiceStatus = {
      ...BASE,
      recording: {
        ...IDLE_RECORDING,
        active: true,
        session: '20261114T030000Z',
        frames: 120,
        sensors: 300,
        dropped: 2,
      },
    };
    expect(row(recording, 'Recording')).toEqual({
      label: 'Recording',
      value: '20261114T030000Z · 120 frames · 300 sensor lines · 2 dropped',
      level: 'ok',
    });

    const failed: ServiceStatus = {
      ...recording,
      recording: { ...recording.recording, error: 'recording failed: disk full' },
    };
    expect(row(failed, 'Recording')?.level).toBe('bad');
    expect(row(failed, 'Recording')?.value).toContain('disk full');
  });

  it('handles a status before any camera hello', () => {
    expect(row({ ...BASE, camera: null }, 'Camera')?.value).toBe('640×480 · 10.0 fps · age 40 ms');
    expect(row({ ...BASE, frame: null }, 'Camera')).toEqual({ label: 'Camera', value: 'No frames yet', level: 'bad' });
  });

  it('omits unknown GPS speed', () => {
    const still: ServiceStatus = { ...BASE, gps: { ...BASE.gps!, speed_mps: null } };
    expect(row(still, 'GPS')?.value).toBe('38.986910, -76.942620 · ±4.2 m · age 300 ms');
  });
});

describe('statusRows before clock sync', () => {
  const unsynced: ServiceStatus = {
    ...BASE,
    clock_synced: false,
    sensors_ok: false,
    frame: { ...BASE.frame!, fresh: false },
    gps: { ...BASE.gps!, fresh: false },
  };

  it('says the phone is waiting for clock sync rather than OK or stale', () => {
    expect(row(unsynced, 'Phone')).toEqual({
      label: 'Phone',
      value: 'Connected, waiting for clock sync',
      level: 'warn',
    });
  });

  it('shows latency as waiting even if a clock estimate is present', () => {
    expect(row(unsynced, 'Latency')).toEqual({ label: 'Latency', value: 'Waiting for clock sync', level: 'warn' });
  });

  it('treats a status without clock_synced as not synced (fail closed)', () => {
    const { clock_synced: _omitted, ...legacy } = BASE;
    expect(row(legacy as ServiceStatus, 'Phone')?.value).toBe('Connected, waiting for clock sync');
  });

  it('still reports a disconnected phone first', () => {
    expect(row({ ...unsynced, phone_connected: false }, 'Phone')?.level).toBe('bad');
  });
});

describe('statusRows heading reference', () => {
  it('labels a magnetic heading and warns, since it cannot be combined with true-north data', () => {
    const magnetic: ServiceStatus = {
      ...BASE,
      heading: { ...BASE.heading!, deg: 95, source: 'compass', reference: 'magnetic', declination_deg: null },
    };
    expect(row(magnetic, 'Heading')).toEqual({
      label: 'Heading',
      value: '95° magnetic from compass (compass 95° ±10°) · declination unknown',
      level: 'warn',
    });
  });

  it('warns when the reference is missing', () => {
    const unknown: ServiceStatus = { ...BASE, heading: { ...BASE.heading!, reference: null, declination_deg: null } };
    expect(row(unknown, 'Heading')?.level).toBe('warn');
    expect(row(unknown, 'Heading')?.value).toBe('90° (reference unknown) from GPS course (compass 95° ±10°)');
  });
});

describe('formatDeclination', () => {
  it('names the direction instead of a sign', () => {
    expect(formatDeclination(-11.04)).toBe('11.0° W');
    expect(formatDeclination(3.25)).toBe('3.3° E');
    expect(formatDeclination(0)).toBe('0.0°');
  });
});

describe('statusRows recording lifecycle', () => {
  const ended: RecordingStatus = {
    ...IDLE_RECORDING,
    session: '20261114T030000Z',
    frames: 9000,
    sensors: 20000,
  };

  it('shows a recording that is finishing', () => {
    const finishing: ServiceStatus = { ...BASE, recording: { ...ended, finishing: true } };
    expect(row(finishing, 'Recording')).toEqual({
      label: 'Recording',
      value: '20261114T030000Z · 9000 frames · 20000 sensor lines · 0 dropped · finishing…',
      level: 'none',
    });
  });

  it('shows a clean end at a limit without flagging it as a failure', () => {
    const limited: ServiceStatus = { ...BASE, recording: { ...ended, end_reason: 'frame limit reached' } };
    expect(row(limited, 'Recording')).toEqual({
      label: 'Recording',
      value: '20261114T030000Z · 9000 frames · 20000 sensor lines · 0 dropped · ended: frame limit reached · stopped',
      level: 'none',
    });
  });

  it('shows the end reason even before a session name is known', () => {
    const limited: ServiceStatus = { ...BASE, recording: { ...IDLE_RECORDING, end_reason: 'disk nearly full' } };
    expect(row(limited, 'Recording')?.value).toContain('ended: disk nearly full');
  });

  it('flags only an error as bad', () => {
    const failed: ServiceStatus = { ...BASE, recording: { ...ended, error: 'write failed' } };
    expect(row(failed, 'Recording')?.level).toBe('bad');
  });
});

describe('frameBadge', () => {
  const frame = { seq: 5, w: 640, h: 480, age_ms: 40, fresh: true };

  it('shows sequence and age while connected', () => {
    expect(frameBadge(frame, true)).toBe('#5 · 40 ms old');
    expect(frameBadge({ ...frame, fresh: false, age_ms: 2500 }, true)).toBe('#5 · 2.5 s old · STALE');
    expect(frameBadge(null, true)).toBe('No frames yet');
  });

  it('never shows a frozen age as current once the viewer is disconnected', () => {
    expect(frameBadge(frame, false)).toBe('#5 · DISCONNECTED');
    expect(frameBadge(frame, false)).not.toContain('ms old');
    expect(frameBadge(null, false)).toBe('No frames yet · DISCONNECTED');
  });
});

describe('recordButtonState', () => {
  const connected = { connected: true, busy: false };

  it('offers start when idle and stop when active', () => {
    expect(recordButtonState(IDLE_RECORDING, connected)).toEqual({
      label: 'Start recording',
      className: 'primary',
      disabled: false,
    });
    expect(recordButtonState({ ...IDLE_RECORDING, active: true }, connected)).toEqual({
      label: 'Stop recording',
      className: 'danger',
      disabled: false,
    });
  });

  it('is disabled while the previous recording is finishing', () => {
    expect(recordButtonState({ ...IDLE_RECORDING, finishing: true }, connected)).toEqual({
      label: 'Finishing recording…',
      className: 'primary',
      disabled: true,
    });
  });

  it('is disabled while a request is in flight, while disconnected, or before any status', () => {
    expect(recordButtonState(IDLE_RECORDING, { connected: true, busy: true }).disabled).toBe(true);
    expect(recordButtonState(IDLE_RECORDING, { connected: false, busy: false }).disabled).toBe(true);
    expect(recordButtonState(null, connected).disabled).toBe(true);
  });
});

describe('parseStatus', () => {
  it('accepts a status message', () => {
    expect(parseStatus(JSON.stringify(BASE))).toEqual(BASE);
  });

  it.each(['{oops', '[]', JSON.stringify({ type: 'pong' }), JSON.stringify({ type: 'status' })])(
    'rejects %s',
    (raw) => {
      expect(parseStatus(raw)).toBeNull();
    },
  );
});
