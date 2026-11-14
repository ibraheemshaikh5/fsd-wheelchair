// Screen Wake Lock wiring. Safari stops the camera and timers when the screen
// sleeps, so any page that must keep running unattended holds a screen lock.

/**
 * Keep the screen on and report whether the lock is held. The browser drops
 * the lock whenever the page is hidden, so take it again on return. At most
 * one lock is ever held: a superseded one is released, and release events
 * from a lock that is no longer current are ignored.
 */
export function keepScreenOn(onChange: (held: boolean) => void): () => void {
  if (!('wakeLock' in navigator)) {
    onChange(false);
    return () => {};
  }
  let sentinel: WakeLockSentinel | null = null;
  let active = true;

  const adopt = (lock: WakeLockSentinel): void => {
    if (!active) {
      void lock.release();
      return;
    }
    const previous = sentinel;
    sentinel = lock;
    if (previous !== null && previous !== lock && !previous.released) void previous.release();
    onChange(true);
    lock.addEventListener('release', () => {
      if (sentinel === lock) onChange(false);
    });
  };

  const acquire = (): void => {
    // Do not use a "pending" flag instead: a request refused because the page
    // was hidden when it ran would then block the visible retry.
    if (sentinel !== null && !sentinel.released) return;
    navigator.wakeLock
      .request('screen')
      .then(adopt)
      .catch(() => {
        if (active) onChange(false);
      });
  };

  const onVisibility = (): void => {
    if (document.visibilityState === 'visible' && active) acquire();
  };
  document.addEventListener('visibilitychange', onVisibility);
  acquire();
  return () => {
    active = false;
    document.removeEventListener('visibilitychange', onVisibility);
    void sentinel?.release();
  };
}
