import { describe, expect, it } from 'vitest';
import { createFramePlayer } from '../../viewer/frame-player';

/** A decoder whose results the test hands out one by one. */
function harness() {
  const decoding: { id: number; resolve: (name: string) => void; reject: (error: Error) => void }[] = [];
  const shown: string[] = [];
  const released: string[] = [];
  const errors: unknown[] = [];
  const player = createFramePlayer<string>({
    decode: (jpeg) =>
      new Promise<string>((resolve, reject) => {
        decoding.push({ id: jpeg.byteLength, resolve, reject });
      }),
    show: (image) => shown.push(image),
    release: (image) => released.push(image),
    onError: (error) => errors.push(error),
  });
  const frame = (id: number): ArrayBuffer => new ArrayBuffer(id);
  /** Finishes the oldest decode in flight and lets the player react. */
  const finish = async (): Promise<number> => {
    const job = decoding.shift();
    if (job === undefined) throw new Error('nothing is decoding');
    job.resolve(`img${job.id}`);
    await Promise.resolve();
    await Promise.resolve();
    return job.id;
  };
  const fail = async (): Promise<void> => {
    decoding.shift()?.reject(new Error('corrupt JPEG'));
    await Promise.resolve();
    await Promise.resolve();
  };
  return { player, decoding, shown, released, errors, frame, finish, fail };
}

describe('createFramePlayer', () => {
  it('shows each frame once decoded and releases the one it replaces', async () => {
    const t = harness();
    t.player.push(t.frame(1));
    await t.finish();
    t.player.push(t.frame(2));
    await t.finish();
    expect(t.shown).toEqual(['img1', 'img2']);
    expect(t.released).toEqual(['img1']);
    expect(t.player.stats()).toEqual({ shown: 2, skipped: 0, failed: 0 });
  });

  it('decodes one frame at a time and skips to the newest when frames come faster', async () => {
    const t = harness();
    t.player.push(t.frame(1));
    t.player.push(t.frame(2));
    t.player.push(t.frame(3));
    expect(t.decoding.map((j) => j.id)).toEqual([1]);
    await t.finish();
    // Frame 2 was overtaken by 3 before it was ever decoded.
    expect(t.decoding.map((j) => j.id)).toEqual([3]);
    await t.finish();
    expect(t.shown).toEqual(['img1', 'img3']);
    expect(t.player.stats()).toEqual({ shown: 2, skipped: 1, failed: 0 });
  });

  it('reports a frame that cannot be decoded and carries on with the next', async () => {
    const t = harness();
    t.player.push(t.frame(1));
    t.player.push(t.frame(2));
    await t.fail();
    expect(t.errors).toHaveLength(1);
    await t.finish();
    expect(t.shown).toEqual(['img2']);
    expect(t.player.stats().failed).toBe(1);
  });

  it('releases everything on close, including a decode that finishes afterwards', async () => {
    const t = harness();
    t.player.push(t.frame(1));
    await t.finish();
    t.player.push(t.frame(2));
    t.player.push(t.frame(3));
    t.player.close();
    expect(t.released).toEqual(['img1']);
    await t.finish();
    expect(t.shown).toEqual(['img1']);
    expect(t.released).toEqual(['img1', 'img2']);
    // Nothing more is decoded or shown once closed.
    t.player.push(t.frame(4));
    expect(t.decoding).toHaveLength(0);
  });
});
