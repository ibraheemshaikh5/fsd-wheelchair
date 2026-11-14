import { describe, expect, it, vi } from 'vitest';
import { AUTO_ROTATION } from '../../capture/orientation';
import { DEFAULT_SETTINGS } from '../../capture/settings';
import { flush, harness, OPEN, startFully, type Harness } from './session-harness';

// iOS: accelerationIncludingGravity points down. Phone turned clockwise: down is +x.
const sideways = { accelerationIncludingGravity: { x: 9.8, y: 0, z: 0 }, rotationRate: null };

function sentHeader(h: Harness): Record<string, unknown> {
  const bytes = h.transport().sendFrame.mock.calls.at(-1)?.[0];
  if (bytes === undefined) throw new Error('no frame sent');
  const length = new DataView(bytes.buffer).getUint32(0, true);
  return JSON.parse(new TextDecoder().decode(bytes.subarray(4, 4 + length))) as Record<string, unknown>;
}

describe('Session frame rotation', () => {
  it('tells the encoder how the phone is turned, from gravity', async () => {
    const h = harness();
    await startFully(h);
    h.transport().state(OPEN);
    h.sensors.motion?.(sideways);
    h.frame.onFrame?.(undefined);
    await flush();
    expect(h.deps.encodeFrame).toHaveBeenLastCalledWith(
      DEFAULT_SETTINGS.maxWidth,
      DEFAULT_SETTINGS.jpegQuality,
      { physical: 90, setting: AUTO_ROTATION },
    );
  });

  it('assumes upright until the motion sensors say otherwise', async () => {
    const h = harness();
    await startFully(h);
    h.transport().state(OPEN);
    h.frame.onFrame?.(undefined);
    await flush();
    expect(h.deps.encodeFrame).toHaveBeenLastCalledWith(expect.any(Number), expect.any(Number), {
      physical: 0,
      setting: AUTO_ROTATION,
    });
  });

  it('passes a fixed rotation setting through', async () => {
    const h = harness({ ...DEFAULT_SETTINGS, rotateDeg: 270 });
    await startFully(h);
    h.transport().state(OPEN);
    h.frame.onFrame?.(undefined);
    await flush();
    expect(h.deps.encodeFrame).toHaveBeenLastCalledWith(expect.any(Number), expect.any(Number), {
      physical: 0,
      setting: 270,
    });
  });

  it('puts the rotation the encoder applied in the frame header', async () => {
    const h = harness();
    vi.mocked(h.deps.encodeFrame).mockResolvedValue({ bytes: new Uint8Array([1, 2]), w: 640, h: 360, rot: 90 });
    await startFully(h);
    h.transport().state(OPEN);
    h.frame.onFrame?.(undefined);
    await flush();
    expect(sentHeader(h)).toMatchObject({ w: 640, h: 360, rot: 90 });
  });
});
