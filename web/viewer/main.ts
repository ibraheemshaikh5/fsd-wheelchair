// Debug viewer: shows what the service is receiving from the phone and
// starts/stops recordings. Runs on the laptop or the iPad.

import { sendRequest } from '../shared/api';
import { byId as pageElement, renderRows } from '../shared/dom';
import { createPositionMap } from '../shared/map';
import type { ServiceStatus } from '../shared/status';
import { connectViewer, type ViewerConnection, viewerSocketUrl } from '../shared/viewer-socket';
import { frameBadge, recordButtonState, statusRows } from './format';

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
let frameUrl: string | null = null;
let lastStatus: ServiceStatus | null = null;
let connected = false;
let busy = false;

function showRecordButton(): void {
  const state = recordButtonState(lastStatus?.recording ?? null, { connected, busy });
  ui.record.textContent = state.label;
  ui.record.className = state.className;
  ui.record.disabled = state.disabled;
}

function render(status: ServiceStatus): void {
  lastStatus = status;
  ui.status.classList.remove('stale');
  renderRows(ui.status, statusRows(status));
  ui.frame.classList.toggle('stale', !(status.frame?.fresh ?? false));
  ui.badge.textContent = frameBadge(status.frame, true);
  map.update(status.gps, status.heading);
  showRecordButton();
}

/** The service is gone: nothing on screen is live any more, so say so everywhere. */
function markDisconnected(): void {
  connected = false;
  ui.frame.classList.add('stale');
  ui.badge.textContent = frameBadge(lastStatus?.frame ?? null, false);
  ui.status.classList.add('stale');
  ui.status.querySelectorAll('dd').forEach((dd) => {
    dd.className = 'level-none';
  });
  map.markStale();
  showRecordButton();
}

function showFrame(data: ArrayBuffer): void {
  const next = URL.createObjectURL(new Blob([data], { type: 'image/jpeg' }));
  ui.frame.src = next;
  if (frameUrl !== null) URL.revokeObjectURL(frameUrl);
  frameUrl = next;
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
  onFrame: showFrame,
});
