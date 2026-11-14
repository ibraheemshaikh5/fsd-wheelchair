// Thin wrappers around the browser sensor APIs. Logic that can be tested
// without a browser lives in the neighbouring modules; this file only wires
// those to Safari/Chrome and is exercised by the Playwright test and on device.

import {
  type CaptureSize,
  type FallbackFrameState,
  type FrameMetadataLike,
  pickRearCamera,
  scaledSize,
  shouldGrabFallbackFrame,
  videoConstraints,
} from './camera';
import type { OrientationLike } from './heading';
import type { PositionLike } from './location';
import type { MotionLike } from './motion';
import type { CameraLike, EncodedFrameLike, Unsubscribe } from './session';
import type { SocketLike } from './transport';

/** Present a browser WebSocket through the narrow interface the Transport uses. */
export function browserSocket(url: string): SocketLike {
  const ws = new WebSocket(url);
  const adapter: SocketLike = {
    get readyState() {
      return ws.readyState;
    },
    get bufferedAmount() {
      return ws.bufferedAmount;
    },
    get binaryType() {
      return ws.binaryType;
    },
    set binaryType(value: BinaryType) {
      ws.binaryType = value;
    },
    send: (data) => ws.send(data),
    close: () => ws.close(),
    onopen: null,
    onclose: null,
    onmessage: null,
    onerror: null,
  };
  ws.onopen = () => adapter.onopen?.();
  ws.onclose = (event) => adapter.onclose?.({ code: event.code, reason: event.reason });
  ws.onmessage = (event: MessageEvent<unknown>) => adapter.onmessage?.({ data: event.data });
  ws.onerror = () => adapter.onerror?.();
  return adapter;
}

interface PermissionRequester {
  requestPermission?: () => Promise<'granted' | 'denied' | 'default'>;
}

/**
 * iOS asks separately for motion and orientation access, and only from a tap.
 * Call this first in the Start handler, before awaiting anything else, or iOS
 * treats the request as not user-initiated and refuses it.
 */
export function requestMotionAccess(): Promise<'granted' | 'denied' | 'not-needed'> {
  const orientation = (globalThis.DeviceOrientationEvent as unknown as PermissionRequester | undefined)
    ?.requestPermission;
  const motion = (globalThis.DeviceMotionEvent as unknown as PermissionRequester | undefined)?.requestPermission;
  if (orientation === undefined && motion === undefined) return Promise.resolve('not-needed');
  const requests = [orientation, motion]
    .filter((request): request is NonNullable<typeof request> => request !== undefined)
    .map((request) => request.call(null).catch(() => 'denied' as const));
  return Promise.all(requests).then((results) =>
    results.every((result) => result === 'granted') ? 'granted' : 'denied',
  );
}

const abortError = (): DOMException => new DOMException('Camera start was cancelled.', 'AbortError');

/** Release a stream that arrived after the caller gave up, and stop there. */
function keepUnlessAborted(stream: MediaStream, signal: AbortSignal): MediaStream {
  if (!signal.aborted) return stream;
  stream.getTracks().forEach((track) => track.stop());
  throw abortError();
}

/**
 * Open the rear camera, preferring the ultra-wide lens. The first request uses
 * facingMode because device labels stay hidden until permission is granted.
 * Once `signal` aborts, any stream obtained is stopped and the call rejects.
 */
export async function openRearCamera(size: CaptureSize, signal: AbortSignal): Promise<CameraLike<MediaStream>> {
  const request = async (deviceId: string | null): Promise<MediaStream> =>
    keepUnlessAborted(
      await navigator.mediaDevices.getUserMedia({ video: videoConstraints(deviceId, size), audio: false }),
      signal,
    );
  const first = await request(null);
  const firstTrack = first.getVideoTracks()[0];
  const best = pickRearCamera(await navigator.mediaDevices.enumerateDevices());
  keepUnlessAborted(first, signal);
  if (best === null || best.deviceId === firstTrack?.getSettings().deviceId) {
    return { stream: first, label: firstTrack?.label ?? 'camera' };
  }
  first.getTracks().forEach((track) => track.stop());
  try {
    return { stream: await request(best.deviceId), label: best.label };
  } catch (error) {
    if (signal.aborted) throw error;
    // The preferred lens refused the constraints; go back to whatever works.
    const fallback = await request(null);
    return { stream: fallback, label: fallback.getVideoTracks()[0]?.label ?? 'camera' };
  }
}

function fallbackFrameState(video: HTMLVideoElement): FallbackFrameState {
  const source = video.srcObject;
  const track = source instanceof MediaStream ? source.getVideoTracks()[0] : undefined;
  return {
    paused: video.paused,
    readyState: video.readyState,
    muted: track?.muted ?? true,
    trackState: track?.readyState ?? 'ended',
  };
}

/** Call onFrame at most `fps` times a second, with frame timing metadata when the browser has it. */
export function startFrameLoop(
  video: HTMLVideoElement,
  fps: number,
  onFrame: (metadata: FrameMetadataLike | undefined) => void,
): Unsubscribe {
  const intervalMs = 1000 / fps;
  if (!('requestVideoFrameCallback' in video)) {
    // No per-frame callback: grab on a timer, but never from a stalled camera.
    const id = window.setInterval(() => {
      if (shouldGrabFallbackFrame(fallbackFrameState(video))) onFrame(undefined);
    }, intervalMs);
    return () => window.clearInterval(id);
  }
  let last = Number.NEGATIVE_INFINITY;
  let handle = 0;
  let running = true;
  const tick = (now: number, metadata: VideoFrameCallbackMetadata): void => {
    if (!running) return;
    // Small tolerance so 30 fps video divides evenly into 10 fps.
    if (now - last >= intervalMs * 0.9) {
      last = now;
      onFrame(metadata);
    }
    handle = video.requestVideoFrameCallback(tick);
  };
  handle = video.requestVideoFrameCallback(tick);
  return () => {
    running = false;
    video.cancelVideoFrameCallback(handle);
  };
}

/** Draw the current video frame, scaled down, and encode it as JPEG. */
export function encodeJpeg(
  video: HTMLVideoElement,
  canvas: HTMLCanvasElement,
  maxSide: number,
  quality: number,
): Promise<EncodedFrameLike | null> {
  if (video.videoWidth === 0 || video.videoHeight === 0) return Promise.resolve(null);
  const { w, h } = scaledSize(video.videoWidth, video.videoHeight, maxSide);
  if (canvas.width !== w) canvas.width = w;
  if (canvas.height !== h) canvas.height = h;
  const context = canvas.getContext('2d');
  if (context === null) return Promise.resolve(null);
  context.drawImage(video, 0, 0, w, h);
  return new Promise((resolve) => {
    canvas.toBlob(
      (blob) => {
        if (blob === null) {
          resolve(null);
          return;
        }
        blob
          .arrayBuffer()
          .then((buffer) => resolve({ bytes: new Uint8Array(buffer), w, h }))
          .catch(() => resolve(null));
      },
      'image/jpeg',
      quality,
    );
  });
}

export function watchLocation(
  onPosition: (position: PositionLike) => void,
  onError: (message: string) => void,
): Unsubscribe {
  if (!('geolocation' in navigator)) {
    onError('This browser has no location access.');
    return () => {};
  }
  const id = navigator.geolocation.watchPosition(
    onPosition,
    (error) => onError(locationErrorMessage(error)),
    { enableHighAccuracy: true, maximumAge: 0, timeout: 15_000 },
  );
  return () => navigator.geolocation.clearWatch(id);
}

function locationErrorMessage(error: GeolocationPositionError): string {
  switch (error.code) {
    case error.PERMISSION_DENIED:
      return 'Location permission denied. Allow it in Settings › Safari › Location (or the page’s aA menu).';
    case error.POSITION_UNAVAILABLE:
      return 'No location fix available yet.';
    case error.TIMEOUT:
      return 'Location timed out; still waiting for a fix.';
    default:
      return `Location error: ${error.message}`;
  }
}

/** Listen to compass readings. Safari fires `deviceorientation`; Chrome's absolute one has its own event. */
export function listenOrientation(onReading: (event: OrientationLike) => void): Unsubscribe {
  const handler = (event: Event): void => onReading(event as unknown as OrientationLike);
  window.addEventListener('deviceorientation', handler);
  window.addEventListener('deviceorientationabsolute', handler);
  return () => {
    window.removeEventListener('deviceorientation', handler);
    window.removeEventListener('deviceorientationabsolute', handler);
  };
}

export function listenMotion(onMotion: (event: MotionLike) => void): Unsubscribe {
  const handler = (event: DeviceMotionEvent): void => onMotion(event);
  window.addEventListener('devicemotion', handler);
  return () => window.removeEventListener('devicemotion', handler);
}
