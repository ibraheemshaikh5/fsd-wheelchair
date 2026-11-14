// Device check: answers the open questions about the mounted iPhone before the
// capture page's defaults are trusted. Results go into docs/open-questions.md.

import { pickRearCamera, videoConstraints } from '../capture/camera';
import { listenMotion, listenOrientation, requestMotionAccess, watchLocation } from '../capture/browser';
import { toHeadingMessage } from '../capture/heading';
import { keepScreenOn } from '../shared/wakelock';
import { EDGE_SAMPLES, courseStats, intervals, keepValid, summarise } from './stats';

const SAMPLE_SECONDS = 20;
const MS_PER_S = 1000;

const byId = <T extends HTMLElement>(id: string): T => {
  const element = document.getElementById(id);
  if (element === null) throw new Error(`device check page is missing #${id}`);
  return element as T;
};

const ui = {
  run: byId<HTMLButtonElement>('run'),
  copy: byId<HTMLButtonElement>('copy'),
  progress: byId<HTMLParagraphElement>('progress'),
  error: byId<HTMLParagraphElement>('error'),
  video: byId<HTMLVideoElement>('video'),
  report: byId<HTMLPreElement>('report'),
};

async function checkCameras(): Promise<Record<string, unknown>> {
  const stream = await navigator.mediaDevices.getUserMedia({
    video: videoConstraints(null, { width: 1280, height: 720, fps: 30 }),
    audio: false,
  });
  const devices = await navigator.mediaDevices.enumerateDevices();
  const cameras = devices.filter((d) => d.kind === 'videoinput').map((d) => d.label || '(hidden label)');
  const chosen = pickRearCamera(devices);
  stream.getTracks().forEach((track) => track.stop());

  const target = await navigator.mediaDevices.getUserMedia({
    video: videoConstraints(chosen?.deviceId ?? null, { width: 1280, height: 720, fps: 30 }),
    audio: false,
  });
  const track = target.getVideoTracks()[0];
  ui.video.srcObject = target;
  await ui.video.play();
  const frameTimes = await sampleFrameCallbacks(ui.video, 2 * MS_PER_S);
  const result = {
    cameras,
    chosen: chosen?.label ?? '(facingMode fallback)',
    settings: track?.getSettings(),
    capabilities: typeof track?.getCapabilities === 'function' ? track.getCapabilities() : 'not supported',
    requestVideoFrameCallback: 'requestVideoFrameCallback' in ui.video,
    frameCallbackHasCaptureTime: frameTimes.hasCaptureTime,
    frameCallbackIntervalsMs: summarise(intervals(frameTimes.times)),
  };
  target.getTracks().forEach((t) => t.stop());
  return result;
}

interface FrameSample {
  readonly times: number[];
  readonly hasCaptureTime: boolean;
}

function sampleFrameCallbacks(video: HTMLVideoElement, durationMs: number): Promise<FrameSample> {
  const withCallback = video as HTMLVideoElement & {
    requestVideoFrameCallback?: (cb: (now: number, meta: { captureTime?: number }) => void) => number;
  };
  if (withCallback.requestVideoFrameCallback === undefined) {
    return Promise.resolve({ times: [], hasCaptureTime: false });
  }
  const request = withCallback.requestVideoFrameCallback.bind(video);
  return new Promise((resolve) => {
    const times: number[] = [];
    let hasCaptureTime = false;
    const start = performance.now();
    const tick = (now: number, meta: { captureTime?: number }): void => {
      times.push(now);
      hasCaptureTime ||= meta.captureTime !== undefined;
      if (now - start < durationMs) request(tick);
      else resolve({ times, hasCaptureTime });
    };
    request(tick);
  });
}

async function sampleSensors(): Promise<Record<string, unknown>> {
  const gpsTimes: number[] = [];
  const accuracies: number[] = [];
  // Safari reports an unknown course or speed as -1 or NaN; those are dropped.
  let courses: readonly number[] = [];
  let speeds: readonly number[] = [];
  const compass: number[] = [];
  const compassAccuracy: number[] = [];
  const motionTimes: number[] = [];
  let absoluteAlpha = false;
  let wakeLock: boolean | null = null;
  const errors: string[] = [];

  const stops = [
    watchLocation(
      (position) => {
        gpsTimes.push(performance.now());
        accuracies.push(position.coords.accuracy);
        courses = keepValid(courses, position.coords.heading);
        speeds = keepValid(speeds, position.coords.speed);
      },
      (message) => errors.push(message),
    ),
    listenOrientation((event) => {
      absoluteAlpha ||= event.absolute;
      const heading = toHeadingMessage(event, Date.now());
      if (heading !== null) {
        compass.push(Math.round(heading.magnetic_deg));
        if (heading.accuracy_deg !== null) compassAccuracy.push(heading.accuracy_deg);
      }
    }),
    listenMotion(() => motionTimes.push(performance.now())),
    keepScreenOn((held) => {
      wakeLock = held;
    }),
  ];

  for (let second = SAMPLE_SECONDS; second > 0; second -= 1) {
    ui.progress.textContent = `Sampling sensors… ${second} s left. Roll the chair forward if you can.`;
    await new Promise((resolve) => window.setTimeout(resolve, MS_PER_S));
  }
  stops.forEach((stop) => stop());

  return {
    gps: {
      fixes: gpsTimes.length,
      intervalMs: summarise(intervals(gpsTimes)),
      accuracyM: summarise(accuracies),
      // An angle: a circular mean (358° and 2° average to 0°), plus raw samples.
      courseDeg: courseStats(courses),
      speedMps: summarise(speeds),
    },
    compass: {
      readings: compass.length,
      // Compare these with the real direction the chair faced.
      first: compass.slice(0, EDGE_SAMPLES),
      last: compass.slice(-EDGE_SAMPLES),
      accuracyDeg: summarise(compassAccuracy),
      absoluteAlphaSeen: absoluteAlpha,
    },
    motionEventsPerSecond: Math.round(motionTimes.length / SAMPLE_SECONDS),
    wakeLock: wakeLock ?? 'unsupported',
    errors,
  };
}

async function run(): Promise<void> {
  const motionAccess = requestMotionAccess();
  ui.run.disabled = true;
  ui.copy.disabled = true;
  ui.error.textContent = '';
  ui.report.textContent = 'Running…';
  const report: Record<string, unknown> = {
    when: new Date().toISOString(),
    userAgent: navigator.userAgent,
    secureContext: window.isSecureContext,
    screen: { width: window.screen.width, height: window.screen.height, orientation: window.screen.orientation?.type },
    motionAccess: await motionAccess,
  };
  try {
    ui.progress.textContent = 'Checking cameras…';
    report.camera = await checkCameras();
    report.sensors = await sampleSensors();
  } catch (error: unknown) {
    const message = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
    report.failure = message;
    ui.error.textContent = message;
  } finally {
    ui.progress.textContent = 'Done.';
    ui.report.textContent = JSON.stringify(report, null, 2);
    ui.run.disabled = false;
    ui.copy.disabled = false;
  }
}

ui.run.addEventListener('click', () => {
  void run();
});
ui.copy.addEventListener('click', () => {
  navigator.clipboard
    .writeText(ui.report.textContent ?? '')
    .then(() => {
      ui.progress.textContent = 'Copied.';
    })
    .catch(() => {
      ui.progress.textContent = 'Copy failed; select the text and copy it by hand.';
    });
});
