import { expect, type APIRequestContext, type Page, test, type WebSocket } from '@playwright/test';

interface StatusEnvelope {
  success: boolean;
  data: {
    phone_connected: boolean;
    clock_synced: boolean;
    sensors_ok: boolean;
    frame: { seq: number; w: number; h: number; fresh: boolean } | null;
    gps: { lat: number; lon: number; accuracy_m: number } | null;
    camera: { label: string } | null;
    clock: { rtt_ms: number } | null;
    stats: { frames: number; rejected: number };
    link: { sent: number; acked: number; in_flight: number; jpeg_quality: number; fresh: boolean } | null;
  };
}

const PHONE_SOCKET = /\/ws\/phone$/;
const START_LAT = 38.9869;
const START_LON = -76.9426;
const WALK_STEP_DEG = 1e-6;
const WALK_INTERVAL_MS = 500;
/** How long the wrapped getUserMedia takes, long enough to tap Stop in between. */
const CAMERA_DELAY_MS = 800;
/** How long to watch for a socket the cancelled start must not open. */
const NO_SOCKET_WAIT_MS = 2_000;
const SYNC_TIMEOUT_MS = 15_000;
const DISCONNECT_TIMEOUT_MS = 5_000;

async function status(request: APIRequestContext): Promise<StatusEnvelope['data']> {
  const response = await request.get('/api/status');
  expect(response.ok()).toBe(true);
  const body = (await response.json()) as StatusEnvelope;
  expect(body.success).toBe(true);
  return body.data;
}

/**
 * A mocked position is reported once and never again, unlike a real phone,
 * so nudge it every half second to keep fixes coming. Returns a stop function.
 */
function walk(page: Page): () => void {
  let step = 0;
  const timer = setInterval(() => {
    step += 1;
    void page
      .context()
      .setGeolocation({ latitude: START_LAT + step * WALK_STEP_DEG, longitude: START_LON, accuracy: 5 });
  }, WALK_INTERVAL_MS);
  return () => clearInterval(timer);
}

/** Pretend the page was hidden (screen locked, app switched) or shown again. */
async function setVisibility(page: Page, state: 'hidden' | 'visible'): Promise<void> {
  await page.evaluate((value) => {
    Object.defineProperty(document, 'visibilityState', { value, configurable: true });
    Object.defineProperty(document, 'hidden', { value: value === 'hidden', configurable: true });
    document.dispatchEvent(new Event('visibilitychange'));
  }, state);
}

async function startStreaming(page: Page, request: APIRequestContext): Promise<void> {
  await page.goto('/capture/');
  await page.getByRole('button', { name: 'Start streaming' }).click();
  await expect(page.locator('#status')).toContainText('Connected', { timeout: SYNC_TIMEOUT_MS });
  // sensors_ok needs a measured ping first: nothing is fresh before clock sync.
  await expect.poll(async () => (await status(request)).sensors_ok, { timeout: SYNC_TIMEOUT_MS }).toBe(true);
}

test('the capture page streams camera and location to the service and the viewer shows them', async ({
  browser,
  page,
  request,
}) => {
  const stopWalking = walk(page);
  try {
    await startStreaming(page, request);
    const live = await status(request);
    expect(live.phone_connected).toBe(true);
    expect(live.clock_synced).toBe(true);
    expect(live.clock).not.toBeNull();
    expect(live.frame?.w).toBeGreaterThan(0);
    expect(live.frame?.w).toBeLessThanOrEqual(640);
    expect(live.gps?.lat).toBeCloseTo(START_LAT, 4);
    expect(live.camera).not.toBeNull();
    expect(live.stats.rejected).toBe(0);

    // The service acks frames, so the page paces itself and reports its link once a second.
    await expect
      .poll(async () => (await status(request)).link?.fresh ?? false, { timeout: SYNC_TIMEOUT_MS })
      .toBe(true);
    const link = (await status(request)).link;
    expect(link?.acked).toBeGreaterThan(0);
    expect(link?.in_flight).toBeLessThanOrEqual(3);
    expect(link?.jpeg_quality).toBeGreaterThan(0);
    await expect(page.locator('#status')).toContainText(/Frame ack\s*\d+ ms/);
    await expect(page.locator('#status')).toContainText(/Send rate\s*\d+ of \d+ fps/);
    expect((await status(request)).stats.rejected).toBe(0);

    // Viewer in a second tab, as on the laptop.
    const viewer = await browser.newPage();
    await viewer.goto('/viewer/');
    await expect(viewer.locator('#connection')).toHaveText('Connected to the service');
    await expect(viewer.locator('#status')).toContainText('Connected, sensors OK');
    await expect(viewer.locator('#frame')).toHaveAttribute('src', /^blob:/);
    await expect(viewer.locator('#frame-badge')).toContainText('#');
    await expect(viewer.locator('.leaflet-marker-icon')).toBeVisible();

    // Record a few seconds from the viewer (the phone is connected and has said hello).
    await viewer.getByRole('button', { name: 'Start recording' }).click();
    await expect(viewer.getByRole('button', { name: 'Stop recording' })).toBeVisible();
    await expect(viewer.locator('#status')).toContainText(/[1-9]\d* frames/, { timeout: 10_000 });
    await viewer.getByRole('button', { name: 'Stop recording' }).click();
    await expect(viewer.getByRole('button', { name: 'Start recording' })).toBeVisible();
    await expect(viewer.locator('#status')).toContainText('stopped');
    await expect(viewer.locator('#error')).toBeEmpty();

    // Stopping the phone makes the service see it gone, and the viewer says so.
    await page.getByRole('button', { name: 'Stop' }).click();
    await expect(viewer.locator('#status')).toContainText('Not connected');
    await viewer.close();
  } finally {
    stopWalking();
  }
});

test('the capture page explains a camera it cannot open', async ({ browser }) => {
  const context = await browser.newContext({ permissions: [] });
  const page = await context.newPage();
  await page.addInitScript(() => {
    navigator.mediaDevices.getUserMedia = () =>
      Promise.reject(new DOMException('denied by test', 'NotAllowedError'));
  });
  await page.goto('/capture/');
  await page.getByRole('button', { name: 'Start streaming' }).click();
  await expect(page.locator('#error')).toContainText('Camera permission denied');
  await expect(page.getByRole('button', { name: 'Start streaming' })).toBeEnabled();
  await context.close();
});

test('Stop during a slow camera start opens no socket and leaves the camera off', async ({ page }) => {
  await page.addInitScript((delayMs) => {
    const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    const opened: MediaStream[] = [];
    Object.assign(window, { openedStreams: opened });
    navigator.mediaDevices.getUserMedia = async (constraints) => {
      const stream = await original(constraints);
      opened.push(stream);
      await new Promise((resolve) => setTimeout(resolve, delayMs));
      return stream;
    };
  }, CAMERA_DELAY_MS);
  const phoneSockets: string[] = [];
  page.on('websocket', (socket) => {
    if (PHONE_SOCKET.test(socket.url())) phoneSockets.push(socket.url());
  });

  await page.goto('/capture/');
  await page.getByRole('button', { name: 'Start streaming' }).click();
  await page.getByRole('button', { name: 'Stop' }).click();
  await page.waitForTimeout(NO_SOCKET_WAIT_MS);

  expect(phoneSockets).toEqual([]);
  await expect(page.getByRole('button', { name: 'Start streaming' })).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Stop' })).toBeDisabled();
  const liveTracks = await page.evaluate(() =>
    (window as unknown as { openedStreams: MediaStream[] }).openedStreams
      .flatMap((stream) => stream.getTracks())
      .filter((track) => track.readyState !== 'ended').length,
  );
  expect(liveTracks).toBe(0);
});

test('hiding the page closes the phone socket and showing it resumes streaming', async ({ page, request }) => {
  const phoneSockets: WebSocket[] = [];
  page.on('websocket', (socket) => {
    if (PHONE_SOCKET.test(socket.url())) phoneSockets.push(socket);
  });
  const stopWalking = walk(page);
  try {
    await startStreaming(page, request);
    const framesBefore = (await status(request)).stats.frames;
    const socketsBefore = phoneSockets.length;
    const current = phoneSockets.at(-1);
    expect(current?.isClosed()).toBe(false);
    const closed = current?.waitForEvent('close', { timeout: DISCONNECT_TIMEOUT_MS });

    await setVisibility(page, 'hidden');
    await closed;
    await expect.poll(async () => (await status(request)).phone_connected, { timeout: DISCONNECT_TIMEOUT_MS }).toBe(false);
    // The camera is released while hidden; Stop stays available to cancel the auto-resume.
    await expect(page.locator('#preview')).toHaveJSProperty('srcObject', null);
    await expect(page.getByRole('button', { name: 'Stop' })).toBeEnabled();

    await setVisibility(page, 'visible');
    await expect.poll(() => phoneSockets.length, { timeout: SYNC_TIMEOUT_MS }).toBeGreaterThan(socketsBefore);
    await expect.poll(async () => (await status(request)).sensors_ok, { timeout: SYNC_TIMEOUT_MS }).toBe(true);
    expect((await status(request)).stats.frames).toBeGreaterThan(framesBefore);

    await page.getByRole('button', { name: 'Stop' }).click();
    await expect.poll(async () => (await status(request)).phone_connected, { timeout: DISCONNECT_TIMEOUT_MS }).toBe(false);
  } finally {
    stopWalking();
  }
});
