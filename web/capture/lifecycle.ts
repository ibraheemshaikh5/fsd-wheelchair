// The capture page's start/stop lifecycle as a pure reducer. main.ts dispatches
// events and runs the effects; the buttons and the message come only from the
// `ui` output, so they can never disagree with what is actually running.

import { TAKEOVER_TEXT } from './status';

export type LifecycleKind = 'idle' | 'starting' | 'running' | 'paused';

export interface Lifecycle {
  readonly kind: LifecycleKind;
  /** Shown to the person holding the phone; empty for none. */
  readonly message: string;
}

export type LifecycleEvent =
  | { readonly type: 'START' }
  | { readonly type: 'STOP' }
  | { readonly type: 'STARTED' }
  | { readonly type: 'START_FAILED'; readonly message: string }
  | { readonly type: 'HIDDEN' }
  | { readonly type: 'VISIBLE' }
  | { readonly type: 'CAMERA_MUTED' }
  | { readonly type: 'CAMERA_UNMUTED' }
  | { readonly type: 'CAMERA_ENDED' }
  | { readonly type: 'PAGEHIDE' }
  | { readonly type: 'TAKEN_OVER' };

/**
 * beginSession: create and start a new session.
 * stopSession: stop the current session; one still starting has its start rejected as cancelled.
 * replayPreview: call play() on the preview again after an interruption.
 */
export type Effect = 'beginSession' | 'stopSession' | 'replayPreview';

export interface LifecycleUi {
  readonly startEnabled: boolean;
  readonly stopEnabled: boolean;
  readonly settingsEnabled: boolean;
  readonly message: string;
}

export interface Transition {
  readonly state: Lifecycle;
  readonly effects: readonly Effect[];
  readonly ui: LifecycleUi;
}

export const INITIAL_LIFECYCLE: Lifecycle = Object.freeze({ kind: 'idle', message: '' });

export const CAMERA_ENDED_MESSAGE = 'The camera stopped. Tap Start to resume.';
export const CAMERA_INTERRUPTED_MESSAGE = 'Camera interrupted';
const PAUSED_MESSAGE = 'Paused while the page is hidden.';

interface Step {
  readonly kind: LifecycleKind;
  readonly message: string;
  readonly effects: readonly Effect[];
}

const to = (kind: LifecycleKind, effects: readonly Effect[] = [], message = ''): Step => ({ kind, message, effects });
const stay = (state: Lifecycle, effects: readonly Effect[] = []): Step => ({ ...state, effects });

function fromIdle(state: Lifecycle, event: LifecycleEvent): Step {
  switch (event.type) {
    case 'START':
      return to('starting', ['beginSession']);
    case 'STARTED':
      // A session that finished starting after the user stopped it.
      return stay(state, ['stopSession']);
    default:
      return stay(state);
  }
}

/** How a starting or running session reacts to everything but its own start finishing. */
function fromActive(state: Lifecycle, event: LifecycleEvent): Step {
  switch (event.type) {
    case 'STOP':
    case 'PAGEHIDE':
      return to('idle', ['stopSession']);
    case 'HIDDEN':
      return to('paused', ['stopSession'], PAUSED_MESSAGE);
    case 'CAMERA_ENDED':
      return to('idle', ['stopSession'], CAMERA_ENDED_MESSAGE);
    case 'TAKEN_OVER':
      return to('idle', ['stopSession'], TAKEOVER_TEXT);
    case 'CAMERA_MUTED':
      return to(state.kind, [], CAMERA_INTERRUPTED_MESSAGE);
    case 'CAMERA_UNMUTED':
      return to(state.kind, ['replayPreview']);
    default:
      return stay(state);
  }
}

function fromStarting(state: Lifecycle, event: LifecycleEvent): Step {
  switch (event.type) {
    case 'STARTED':
      return to('running', [], state.message);
    case 'START_FAILED':
      return to('idle', ['stopSession'], event.message);
    default:
      return fromActive(state, event);
  }
}

function fromPaused(state: Lifecycle, event: LifecycleEvent): Step {
  switch (event.type) {
    case 'VISIBLE':
      // Restart on return; no tap needed, since permissions were already granted.
      return to('starting', ['beginSession']);
    case 'STOP':
    case 'PAGEHIDE':
      return to('idle');
    case 'STARTED':
      return stay(state, ['stopSession']);
    default:
      return stay(state);
  }
}

const HANDLERS: Record<LifecycleKind, (state: Lifecycle, event: LifecycleEvent) => Step> = {
  idle: fromIdle,
  starting: fromStarting,
  running: fromActive,
  paused: fromPaused,
};

export function uiFor(state: Lifecycle): LifecycleUi {
  const idle = state.kind === 'idle';
  return { startEnabled: idle, stopEnabled: !idle, settingsEnabled: idle, message: state.message };
}

export function next(state: Lifecycle, event: LifecycleEvent): Transition {
  const step = HANDLERS[state.kind](state, event);
  const nextState: Lifecycle = { kind: step.kind, message: step.message };
  return { state: nextState, effects: step.effects, ui: uiFor(nextState) };
}
