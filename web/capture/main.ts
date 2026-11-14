// Capture page: runs on the phone mounted on the chair and streams camera,
// GPS, compass and IMU to the driving service over one WebSocket.
// This file only wires the page to the lifecycle reducer and the Session.

import { keepScreenOn } from '../shared/wakelock';
import {
  browserSocket,
  encodeJpeg,
  listenMotion,
  listenOrientation,
  openRearCamera,
  requestMotionAccess,
  startFrameLoop,
  watchLocation,
} from './browser';
import {
  type Effect,
  INITIAL_LIFECYCLE,
  type Lifecycle,
  type LifecycleEvent,
  type LifecycleUi,
  next,
  uiFor,
} from './lifecycle';
import { captureStatusRows, INITIAL_LIVE, type LiveState, type MotionAccess, stoppedLive } from './live';
import { SEND_BUDGET } from './send-budget';
import { Session, SessionCancelled, type SessionDeps } from './session';
import {
  type CaptureSettings,
  DEFAULT_SETTINGS,
  parseSettings,
  phoneSocketUrl,
  serializeSettings,
  validSettings,
} from './settings';
import { describeStartError } from './status';
import { Transport } from './transport';

const SETTINGS_KEY = 'fsd-capture-settings';
const STATUS_INTERVAL_MS = 500;
const PREVIEW_RESUME_FAILED = 'The camera preview did not resume. Tap Stop, then Start.';

const byId = <T extends HTMLElement>(id: string): T => {
  const element = document.getElementById(id);
  if (element === null) throw new Error(`capture page is missing #${id}`);
  return element as T;
};

const ui = {
  start: byId<HTMLButtonElement>('start'),
  stop: byId<HTMLButtonElement>('stop'),
  error: byId<HTMLParagraphElement>('error'),
  insecure: byId<HTMLParagraphElement>('insecure'),
  status: byId<HTMLDListElement>('status'),
  preview: byId<HTMLVideoElement>('preview'),
  encoder: byId<HTMLCanvasElement>('encoder'),
  settings: byId<HTMLFormElement>('settings'),
};

let lifecycle: Lifecycle = INITIAL_LIFECYCLE;
let session: Session<MediaStream> | null = null;
/** Shown while no session is running. */
let idleLive: LiveState = INITIAL_LIVE;
let settings: CaptureSettings = loadSettings();
/** The last motion-permission answer, reused when the page restarts itself without a tap. */
let motionAccess: Promise<MotionAccess> = Promise.resolve('denied');

function loadSettings(): CaptureSettings {
  try {
    return parseSettings(window.localStorage.getItem(SETTINGS_KEY));
  } catch {
    return DEFAULT_SETTINGS;
  }
}

function saveSettings(next: CaptureSettings): void {
  try {
    window.localStorage.setItem(SETTINGS_KEY, serializeSettings(next));
  } catch {
    // Private browsing or storage blocked: settings just won't persist.
  }
}

function showError(message: string): void {
  ui.error.textContent = message;
}

const browserDeps: SessionDeps<MediaStream> = {
  openCamera: openRearCamera,
  playPreview: async (stream) => {
    ui.preview.srcObject = stream;
    await ui.preview.play();
    // Read once playing: the rate the camera actually runs at, which iOS may set below the request.
    const track = stream.getVideoTracks()[0]?.getSettings();
    return {
      width: ui.preview.videoWidth || (track?.width ?? 0),
      height: ui.preview.videoHeight || (track?.height ?? 0),
      frameRate: track?.frameRate,
    };
  },
  clearPreview: (stream) => {
    if (ui.preview.srcObject === stream) ui.preview.srcObject = null;
  },
  resumePreview: () => ui.preview.play(),
  createTransport: (handlers) =>
    new Transport(phoneSocketUrl(window.location), {
      createSocket: browserSocket,
      setTimer: (fn, ms) => window.setTimeout(fn, ms),
      clearTimer: (id) => window.clearTimeout(id),
      onState: handlers.onState,
      onMessage: handlers.onMessage,
      sendBudget: SEND_BUDGET,
      now: Date.now,
    }),
  startFrameLoop: (intervalMs, onFrame) => startFrameLoop(ui.preview, intervalMs, onFrame),
  encodeFrame: (maxSide, quality, orientation) => encodeJpeg(ui.preview, ui.encoder, maxSide, quality, orientation),
  watchLocation,
  keepScreenOn,
  listenOrientation,
  listenMotion,
  setInterval: (fn, ms) => window.setInterval(fn, ms),
  clearInterval: (id) => window.clearInterval(id),
  now: Date.now,
  monotonicNow: () => performance.now(),
  userAgent: navigator.userAgent,
};

const CAMERA_EVENTS = { muted: 'CAMERA_MUTED', unmuted: 'CAMERA_UNMUTED', ended: 'CAMERA_ENDED' } as const;

function beginSession(): void {
  const created: Session<MediaStream> = new Session(settings, browserDeps, {
    onCamera: (change) => {
      if (session === created) dispatch({ type: CAMERA_EVENTS[change] });
    },
    onTakenOver: (message) => {
      if (session === created) dispatch({ type: 'TAKEN_OVER', message });
    },
    onError: (message) => {
      if (session === created) showError(message);
    },
  });
  session = created;
  created.start(motionAccess).then(
    () => {
      if (session === created) dispatch({ type: 'STARTED' });
    },
    (error: unknown) => {
      if (error instanceof SessionCancelled || session !== created) return;
      dispatch({ type: 'START_FAILED', message: describeStartError(error) });
    },
  );
}

function endSession(): void {
  const ending = session;
  session = null;
  if (ending === null) return;
  idleLive = stoppedLive(ending.live);
  ending.stop();
}

function runEffect(effect: Effect): void {
  switch (effect) {
    case 'beginSession':
      beginSession();
      return;
    case 'stopSession':
      endSession();
      return;
    case 'replayPreview':
      session?.replayPreview().catch(() => showError(PREVIEW_RESUME_FAILED));
      return;
  }
}

function applyUi(view: LifecycleUi, showMessage: boolean): void {
  ui.start.disabled = !view.startEnabled;
  ui.stop.disabled = !view.stopEnabled;
  for (const element of Array.from(ui.settings.elements)) {
    if (element instanceof HTMLInputElement || element instanceof HTMLSelectElement) {
      element.disabled = !view.settingsEnabled;
    }
  }
  // Only on a real change, so an ignored event does not wipe a sensor error.
  if (showMessage) showError(view.message);
}

function dispatch(event: LifecycleEvent): void {
  const previous = lifecycle;
  const transition = next(previous, event);
  lifecycle = transition.state;
  const changed = previous.kind !== lifecycle.kind || previous.message !== lifecycle.message;
  // Show the new state first: an effect may dispatch again (e.g. a failed start).
  applyUi(transition.ui, changed);
  transition.effects.forEach(runEffect);
  renderStatus();
}

function renderStatus(): void {
  const rows = captureStatusRows(session?.live ?? idleLive);
  ui.status.replaceChildren(
    ...rows.flatMap(({ label, value, level }) => {
      const dt = document.createElement('dt');
      dt.textContent = label;
      const dd = document.createElement('dd');
      dd.textContent = value;
      dd.className = `level-${level}`;
      return [dt, dd];
    }),
  );
}

function fillSettingsForm(values: CaptureSettings): void {
  for (const [key, value] of Object.entries(values)) {
    const input = ui.settings.elements.namedItem(key);
    if (input instanceof HTMLInputElement || input instanceof HTMLSelectElement) input.value = String(value);
  }
}

function readSettingsForm(): CaptureSettings {
  const raw = Object.fromEntries(
    Object.keys(DEFAULT_SETTINGS).map((key) => {
      const input = ui.settings.elements.namedItem(key);
      const field = input instanceof HTMLInputElement || input instanceof HTMLSelectElement ? input : null;
      return [key, field === null ? Number.NaN : Number(field.value)];
    }),
  );
  return validSettings(raw);
}

function onStart(): void {
  // Must be the first thing in the tap handler; see requestMotionAccess.
  motionAccess = requestMotionAccess();
  settings = readSettingsForm();
  saveSettings(settings);
  fillSettingsForm(settings);
  idleLive = INITIAL_LIVE;
  dispatch({ type: 'START' });
}

function onVisibilityChange(): void {
  dispatch({ type: document.visibilityState === 'hidden' ? 'HIDDEN' : 'VISIBLE' });
}

function onPageShow(event: PageTransitionEvent): void {
  // Restored from the back/forward cache: nothing from before is live any more.
  if (event.persisted) {
    idleLive = INITIAL_LIVE;
    renderStatus();
  }
}

function init(): void {
  if (!window.isSecureContext) {
    ui.insecure.textContent =
      'This page is not on HTTPS, so Safari will block the camera, location and compass. ' +
      'Open it through the Tailscale https:// address instead.';
  }
  fillSettingsForm(settings);
  ui.settings.addEventListener('change', () => {
    settings = readSettingsForm();
    saveSettings(settings);
  });
  ui.start.addEventListener('click', onStart);
  ui.stop.addEventListener('click', () => dispatch({ type: 'STOP' }));
  document.addEventListener('visibilitychange', onVisibilityChange);
  window.addEventListener('pagehide', () => dispatch({ type: 'PAGEHIDE' }));
  window.addEventListener('pageshow', onPageShow);
  applyUi(uiFor(lifecycle), true);
  renderStatus();
  window.setInterval(renderStatus, STATUS_INTERVAL_MS);
}

init();
