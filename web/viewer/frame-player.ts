// Shows camera frames as fast as the viewer can decode them, up to the rate they
// arrive (30 fps or more). One frame decodes at a time; a frame that arrives
// while another is decoding waits, and a newer one replaces it, so a slow tab
// skips frames instead of falling further and further behind.

export interface FramePlayerOptions<T> {
  /** Decodes a JPEG into something `show` can put on screen. */
  readonly decode: (jpeg: ArrayBuffer) => Promise<T>;
  readonly show: (image: T) => void;
  /** Frees an image that is no longer on screen (or never will be). */
  readonly release: (image: T) => void;
  /** A frame could not be decoded; the player carries on with the next. */
  readonly onError: (error: unknown) => void;
}

export interface FramePlayerStats {
  readonly shown: number;
  /** Overtaken by a newer frame before being decoded. */
  readonly skipped: number;
  readonly failed: number;
}

export interface FramePlayer {
  push(jpeg: ArrayBuffer): void;
  stats(): FramePlayerStats;
  /** Releases the image on screen; frames pushed afterwards are ignored. */
  close(): void;
}

export function createFramePlayer<T>(options: FramePlayerOptions<T>): FramePlayer {
  let shown: T | null = null;
  let pending: ArrayBuffer | null = null;
  let decoding = false;
  let closed = false;
  let stats: FramePlayerStats = { shown: 0, skipped: 0, failed: 0 };

  const display = (image: T): void => {
    if (closed) {
      options.release(image);
      return;
    }
    options.show(image);
    if (shown !== null) options.release(shown);
    shown = image;
    stats = { ...stats, shown: stats.shown + 1 };
  };

  const fail = (error: unknown): void => {
    stats = { ...stats, failed: stats.failed + 1 };
    options.onError(error);
  };

  const next = (): void => {
    if (closed || decoding || pending === null) return;
    const jpeg = pending;
    pending = null;
    decoding = true;
    options
      .decode(jpeg)
      .then(display, fail)
      .finally(() => {
        decoding = false;
        next();
      });
  };

  return {
    push(jpeg) {
      if (closed) return;
      if (pending !== null) stats = { ...stats, skipped: stats.skipped + 1 };
      pending = jpeg;
      next();
    },
    stats: () => stats,
    close() {
      closed = true;
      pending = null;
      if (shown !== null) options.release(shown);
      shown = null;
    },
  };
}
