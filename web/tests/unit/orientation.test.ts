import { describe, expect, it } from 'vitest';
import {
  AUTO_ROTATION,
  drawTransform,
  frameRotation,
  INITIAL_GRAVITY,
  physicalRotation,
  rotatedSize,
  updateGravity,
} from '../../capture/orientation';

// iOS Safari reports accelerationIncludingGravity as the gravity vector itself,
// pointing DOWN (an upright phone reads y = -9.8). See drive_service/motion.py.
const G = 9.8;
const upright = { x: 0, y: -G };
const topLeft = { x: -G, y: 0 }; // turned counter-clockwise: the right edge points up
const topRight = { x: G, y: 0 }; // turned clockwise: the left edge points up
const upsideDown = { x: 0, y: G };

const settle = (reading: { x: number; y: number }) => {
  let gravity = INITIAL_GRAVITY;
  for (let i = 0; i < 120; i += 1) gravity = updateGravity(gravity, reading.x, reading.y, 16);
  return gravity;
};

describe('physicalRotation: how far to turn the image clockwise so up is up', () => {
  it('maps each way the phone can be held to a quarter turn', () => {
    expect(physicalRotation(settle(upright), 0)).toBe(0);
    expect(physicalRotation(settle(topRight), 0)).toBe(90);
    expect(physicalRotation(settle(upsideDown), 0)).toBe(180);
    expect(physicalRotation(settle(topLeft), 0)).toBe(270);
  });

  it('does not flip on a tilt short of clearly past the halfway point', () => {
    const tilted50 = { x: G * Math.sin((50 * Math.PI) / 180), y: -G * Math.cos((50 * Math.PI) / 180) };
    expect(physicalRotation(settle(tilted50), 0)).toBe(0);
    const tilted70 = { x: G * Math.sin((70 * Math.PI) / 180), y: -G * Math.cos((70 * Math.PI) / 180) };
    expect(physicalRotation(settle(tilted70), 0)).toBe(90);
  });

  it('keeps the last rotation while the phone lies flat or nothing is known yet', () => {
    expect(physicalRotation(settle({ x: 0.3, y: -0.2 }), 90)).toBe(90);
    expect(physicalRotation(INITIAL_GRAVITY, 270)).toBe(270);
  });
});

describe('updateGravity', () => {
  it('smooths out a jolt from walking', () => {
    const steady = settle(upright);
    const jolted = updateGravity(steady, G, 0, 16);
    expect(physicalRotation(jolted, 0)).toBe(0);
  });

  it('ignores missing readings and bad time steps', () => {
    const steady = settle(upright);
    expect(updateGravity(steady, Number.NaN, 0, 16)).toBe(steady);
    expect(updateGravity(steady, 0, -G, -5)).toBe(steady);
  });
});

describe('frameRotation', () => {
  it('rotates a portrait video when the phone is sideways (Portrait Orientation Lock on)', () => {
    expect(frameRotation(720, 1280, 90, AUTO_ROTATION)).toBe(90);
    expect(frameRotation(720, 1280, 270, AUTO_ROTATION)).toBe(270);
  });

  it('leaves video Safari already turned to landscape alone', () => {
    expect(frameRotation(1280, 720, 90, AUTO_ROTATION)).toBe(0);
  });

  it('leaves an upright phone portrait (it cannot become landscape without cropping)', () => {
    expect(frameRotation(720, 1280, 0, AUTO_ROTATION)).toBe(0);
  });

  it('obeys a fixed setting over the sensors', () => {
    expect(frameRotation(1280, 720, 0, 180)).toBe(180);
    expect(frameRotation(720, 1280, 90, 0)).toBe(0);
  });
});

describe('rotatedSize and drawTransform', () => {
  const apply = (m: readonly number[], x: number, y: number): number[] => {
    const [a = 0, b = 0, c = 0, d = 0, e = 0, f = 0] = m;
    return [a * x + c * y + e, b * x + d * y + f];
  };

  it('swaps width and height for quarter turns', () => {
    expect(rotatedSize(360, 640, 90)).toEqual({ w: 640, h: 360 });
    expect(rotatedSize(360, 640, 270)).toEqual({ w: 640, h: 360 });
    expect(rotatedSize(360, 640, 180)).toEqual({ w: 360, h: 640 });
  });

  it('maps the image corners onto the canvas for every rotation', () => {
    const w = 360;
    const h = 640;
    // Clockwise 90: the source's top-left corner ends up top-right.
    expect(apply(drawTransform(90, w, h), 0, 0)).toEqual([h, 0]);
    expect(apply(drawTransform(90, w, h), w, h)).toEqual([0, w]);
    // Clockwise 270: top-left ends up bottom-left.
    expect(apply(drawTransform(270, w, h), 0, 0)).toEqual([0, w]);
    expect(apply(drawTransform(270, w, h), w, h)).toEqual([h, 0]);
    expect(apply(drawTransform(180, w, h), 0, 0)).toEqual([w, h]);
    expect(apply(drawTransform(0, w, h), 10, 20)).toEqual([10, 20]);
  });
});
