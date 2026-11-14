import { afterEach, describe, expect, it, vi } from 'vitest';
import { keepScreenOn } from '../../shared/wakelock';

class FakeLock {
  released = false;
  private readonly listeners: (() => void)[] = [];

  addEventListener(_type: 'release', listener: () => void): void {
    this.listeners.push(listener);
  }

  release(): Promise<void> {
    if (!this.released) {
      this.released = true;
      this.listeners.forEach((listener) => listener());
    }
    return Promise.resolve();
  }
}

interface Pending {
  resolve(lock: FakeLock): void;
  reject(error: unknown): void;
}

function installFakes(): { pending: Pending[]; locks: FakeLock[]; setVisibility(state: 'visible' | 'hidden'): void } {
  const pending: Pending[] = [];
  const locks: FakeLock[] = [];
  const listeners = new Set<() => void>();
  const fakeDocument = {
    visibilityState: 'visible' as 'visible' | 'hidden',
    addEventListener: (_type: string, listener: () => void) => listeners.add(listener),
    removeEventListener: (_type: string, listener: () => void) => listeners.delete(listener),
  };
  vi.stubGlobal('document', fakeDocument);
  vi.stubGlobal('navigator', {
    wakeLock: {
      request: () =>
        new Promise<FakeLock>((resolve, reject) => {
          pending.push({
            resolve: (lock) => {
              locks.push(lock);
              resolve(lock);
            },
            reject,
          });
        }),
    },
  });
  return {
    pending,
    locks,
    setVisibility: (state) => {
      fakeDocument.visibilityState = state;
      listeners.forEach((listener) => listener());
    },
  };
}

const flush = async (): Promise<void> => {
  for (let i = 0; i < 10; i += 1) await Promise.resolve();
};

describe('keepScreenOn', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('reports not held when the browser has no wake lock', () => {
    vi.stubGlobal('navigator', {});
    const onChange = vi.fn();
    const release = keepScreenOn(onChange);
    expect(onChange).toHaveBeenCalledWith(false);
    release();
  });

  it('never holds two locks after a quick hide and show, and releases them all on stop', async () => {
    const fakes = installFakes();
    const onChange = vi.fn();
    const release = keepScreenOn(onChange);
    fakes.setVisibility('hidden');
    fakes.setVisibility('visible');
    fakes.pending.forEach((request) => request.resolve(new FakeLock()));
    await flush();
    expect(fakes.locks.filter((lock) => !lock.released).length).toBeLessThanOrEqual(1);
    expect(onChange).toHaveBeenLastCalledWith(true);

    release();
    await flush();
    expect(fakes.locks.every((lock) => lock.released)).toBe(true);
  });

  it('takes the lock again when the page returns after the browser released it', async () => {
    const fakes = installFakes();
    const onChange = vi.fn();
    keepScreenOn(onChange);
    fakes.pending[0]?.resolve(new FakeLock());
    await flush();
    await fakes.locks[0]?.release();
    expect(onChange).toHaveBeenLastCalledWith(false);
    fakes.setVisibility('visible');
    fakes.pending[1]?.resolve(new FakeLock());
    await flush();
    expect(onChange).toHaveBeenLastCalledWith(true);
  });

  it('does not ask again while a lock is held', async () => {
    const fakes = installFakes();
    keepScreenOn(vi.fn());
    fakes.pending[0]?.resolve(new FakeLock());
    await flush();
    fakes.setVisibility('visible');
    expect(fakes.pending).toHaveLength(1);
  });

  it('reports a refused request and ignores it after stop', async () => {
    const fakes = installFakes();
    const onChange = vi.fn();
    const release = keepScreenOn(onChange);
    fakes.pending[0]?.reject(new DOMException('hidden', 'NotAllowedError'));
    await flush();
    expect(onChange).toHaveBeenLastCalledWith(false);

    fakes.setVisibility('visible');
    release();
    fakes.pending[1]?.reject(new DOMException('hidden', 'NotAllowedError'));
    fakes.setVisibility('visible');
    await flush();
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(fakes.pending).toHaveLength(2);
  });

  it('releases a lock granted after stop', async () => {
    const fakes = installFakes();
    const onChange = vi.fn();
    const release = keepScreenOn(onChange);
    release();
    fakes.pending[0]?.resolve(new FakeLock());
    await flush();
    expect(fakes.locks[0]?.released).toBe(true);
    expect(onChange).not.toHaveBeenCalled();
  });
});
