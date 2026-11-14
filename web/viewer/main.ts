// Debug viewer: shows what the service is receiving from the phone and
// starts/stops recordings. Runs on the laptop or the iPad.

import { sendRequest } from '../shared/api';
import { byId as pageElement, renderRows } from '../shared/dom';
import { createPositionMap } from '../shared/map';
import type { ServiceStatus } from '../shared/status';
import { connectViewer, type ViewerConnection, viewerSocketUrl } from '../shared/viewer-socket';
import { frameBadge, recordButtonState, statusRows } from './format';
import { createFramePlayer } from './frame-player';
import { ROWS_REDRAW_MS, createChangeGate, createRowsGate, mapUpdateKey } from './render-gate';

const byId = <T extends HTMLElement>(id: string): T => pageElement<T>('viewer', id);

const ui = {
  connection: byId<HTMLParagraphElement>('connection'),
  frame: byId<HTMLImageElement>('frame'),
  badge: byId<HTMLSpanElement>('frame-badge'),
  status: byId<HTMLDListElement>('status'),
  record: byId<HTMLButtonElement>('record'),
  error: byId<HTMLParagraphElement>('error'),
};

const map = createPositionMap(byId('map'));
// Redraw the list and the map only when what they show changes (statuses come ~30 a second).
// The list's ages and fps change on every status, so its text redraws a few times a second.
const newRowsGate = () => createRowsGate(ROWS_REDRAW_MS, () => performance.now());
let rowsChanged = newRowsGate();
let mapChanged = createChangeGate();
let lastStatus: ServiceStatus | null = null;
let connected = false;
let busy = false;

function showRecordButton(): void {
  const state = recordButtonState(lastStatus?.recording ?? null, { connected, busy });
  ui.record.textContent = state.label;
  ui.record.className = state.className;
  ui.record.disabled = state.disabled;
}

/** Decodes a frame off screen, so swapping it into the page never shows a half-loaded image. */
async function decodeFrame(jpeg: ArrayBuffer): Promise<string> {
  const url = URL.createObjectURL(new Blob([jpeg], { type: 'image/jpeg' }));
  try {
    const image = new Image();
    image.src = url;
    await image.decode();
    return url;
  } catch (error) {
    URL.revokeObjectURL(url);
    throw error;
  }
}

const showBadge = (): void => {
  ui.badge.textContent = frameBadge(lastStatus?.frame ?? null, connected, player.stats().failed);
};

const player = createFramePlayer<string>({
  decode: decodeFrame,
  show: (url) => {
    ui.frame.src = url;
  },
  release: (url) => URL.revokeObjectURL(url),
  // The bad frame is skipped and counted on the badge; the next one replaces it.
  onError: showBadge,
});

function render(status: ServiceStatus): void {
  lastStatus = status;
  ui.status.classList.remove('stale');
  const rows = statusRows(status);
  if (rowsChanged(rows)) renderRows(ui.status, rows);
  ui.frame.classList.toggle('stale', !(status.frame?.fresh ?? false));
  showBadge();
  if (mapChanged(mapUpdateKey(status.gps, status.heading))) map.update(status.gps, status.heading);
  showRecordButton();
}

/** The service is gone: nothing on screen is live any more, so say so everywhere. */
function markDisconnected(): void {
  connected = false;
  // Everything below is dimmed now, so the next status must redraw it even if unchanged.
  rowsChanged = newRowsGate();
  mapChanged = createChangeGate();
  ui.frame.classList.add('stale');
  showBadge();
  ui.status.classList.add('stale');
  ui.status.querySelectorAll('dd').forEach((dd) => {
    dd.className = 'level-none';
  });
  map.markStale();
  showRecordButton();
}

function showConnection(state: ViewerConnection): void {
  if (state.kind === 'open') {
    connected = true;
    showRecordButton();
    ui.connection.textContent = 'Connected to the service';
    ui.connection.className = 'level-ok';
  } else if (state.kind === 'lost') {
    ui.connection.textContent = `Service ${state.reason}; reconnecting…`;
    ui.connection.className = 'level-bad';
    markDisconnected();
  }
}

async function toggleRecording(): Promise<void> {
  const action = lastStatus?.recording.active === true ? 'stop' : 'start';
  busy = true;
  showRecordButton();
  ui.error.textContent = '';
  try {
    const error = await sendRequest(`${action} recording`, `/api/recording/${action}`, 'POST', undefined);
    ui.error.textContent = error ?? '';
  } finally {
    busy = false;
    showRecordButton();
  }
}

ui.record.addEventListener('click', () => {
  void toggleRecording();
});
showRecordButton();
connectViewer({
  url: viewerSocketUrl(window.location),
  createSocket: (url) => new WebSocket(url),
  setTimer: (fn, ms) => window.setTimeout(fn, ms),
  clearTimer: (id) => window.clearTimeout(id),
  onConnection: showConnection,
  onStatus: render,
  onFrame: (jpeg) => player.push(jpeg),
});
