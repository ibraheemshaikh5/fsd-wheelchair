import { describe, expect, it } from 'vitest';
import {
  CAMERA_ENDED_MESSAGE,
  CAMERA_INTERRUPTED_MESSAGE,
  INITIAL_LIFECYCLE,
  type Lifecycle,
  type LifecycleKind,
  next,
  uiFor,
} from '../../capture/lifecycle';
import { TAKEOVER_TEXT } from '../../capture/status';

const at = (kind: LifecycleKind, message = ''): Lifecycle => ({ kind, message });

describe('lifecycle reducer', () => {
  it('starts idle with only Start and the settings enabled', () => {
    expect(INITIAL_LIFECYCLE.kind).toBe('idle');
    expect(uiFor(INITIAL_LIFECYCLE)).toEqual({
      startEnabled: true,
      stopEnabled: false,
      settingsEnabled: true,
      message: '',
    });
  });

  it('begins a session on Start and keeps Stop enabled while starting', () => {
    const result = next(at('idle', 'old error'), { type: 'START' });
    expect(result.state).toEqual({ kind: 'starting', message: '' });
    expect(result.effects).toEqual(['beginSession']);
    expect(result.ui).toEqual({ startEnabled: false, stopEnabled: true, settingsEnabled: false, message: '' });
  });

  it('stops a session that is still starting when Stop is tapped', () => {
    const result = next(at('starting'), { type: 'STOP' });
    expect(result.state.kind).toBe('idle');
    expect(result.effects).toEqual(['stopSession']);
    expect(result.ui.startEnabled).toBe(true);
  });

  it('stops a session that finishes starting after the user already stopped', () => {
    const result = next(at('idle'), { type: 'STARTED' });
    expect(result.state.kind).toBe('idle');
    expect(result.effects).toEqual(['stopSession']);
  });

  it('runs once the session has started', () => {
    const result = next(at('starting'), { type: 'STARTED' });
    expect(result.state.kind).toBe('running');
    expect(result.effects).toEqual([]);
    expect(result.ui.stopEnabled).toBe(true);
  });

  it('shows why a start failed and lets the user try again', () => {
    const result = next(at('starting'), { type: 'START_FAILED', message: 'Camera permission denied.' });
    expect(result.state).toEqual({ kind: 'idle', message: 'Camera permission denied.' });
    expect(result.effects).toEqual(['stopSession']);
    expect(result.ui.startEnabled).toBe(true);
    expect(result.ui.message).toBe('Camera permission denied.');
  });

  it('stops on Stop and on pagehide while running', () => {
    for (const type of ['STOP', 'PAGEHIDE'] as const) {
      const result = next(at('running'), { type });
      expect(result.state.kind).toBe('idle');
      expect(result.effects).toEqual(['stopSession']);
    }
  });

  it('pauses when the page is hidden and restarts when it is visible again', () => {
    expect(next(at('running'), { type: 'HIDDEN' })).toMatchObject({
      state: { kind: 'paused' },
      effects: ['stopSession'],
    });
    expect(next(at('starting'), { type: 'HIDDEN' })).toMatchObject({
      state: { kind: 'paused' },
      effects: ['stopSession'],
    });
    expect(next(at('paused'), { type: 'VISIBLE' })).toMatchObject({
      state: { kind: 'starting' },
      effects: ['beginSession'],
    });
  });

  it('respects an explicit Stop: becoming visible while idle does nothing', () => {
    const result = next(at('idle'), { type: 'VISIBLE' });
    expect(result.state.kind).toBe('idle');
    expect(result.effects).toEqual([]);
  });

  it('stays stopped when Stop is tapped while paused', () => {
    const result = next(at('paused'), { type: 'STOP' });
    expect(result.state.kind).toBe('idle');
    expect(result.effects).toEqual([]);
    expect(result.ui.startEnabled).toBe(true);
  });

  it('ends the session and re-enables Start when the camera stops', () => {
    const result = next(at('running'), { type: 'CAMERA_ENDED' });
    expect(result.state.kind).toBe('idle');
    expect(result.effects).toEqual(['stopSession']);
    expect(result.ui.startEnabled).toBe(true);
    expect(result.ui.stopEnabled).toBe(false);
    expect(result.ui.message).toBe(CAMERA_ENDED_MESSAGE);
    expect(result.ui.message).toContain('Tap Start');
  });

  it('stops a starting session whose camera stops', () => {
    const result = next(at('starting'), { type: 'CAMERA_ENDED' });
    expect(result.state).toEqual({ kind: 'idle', message: CAMERA_ENDED_MESSAGE });
    expect(result.effects).toEqual(['stopSession']);
  });

  it('says the camera is interrupted while muted and replays the preview on unmute', () => {
    const muted = next(at('running'), { type: 'CAMERA_MUTED' });
    expect(muted.state.kind).toBe('running');
    expect(muted.ui.message).toBe(CAMERA_INTERRUPTED_MESSAGE);
    expect(muted.ui.message.toLowerCase()).toContain('interrupted');
    expect(muted.effects).toEqual([]);

    const unmuted = next(muted.state, { type: 'CAMERA_UNMUTED' });
    expect(unmuted.state).toEqual({ kind: 'running', message: '' });
    expect(unmuted.effects).toEqual(['replayPreview']);

    expect(next(at('starting'), { type: 'CAMERA_MUTED' }).state).toEqual({
      kind: 'starting',
      message: CAMERA_INTERRUPTED_MESSAGE,
    });
    expect(next(at('starting'), { type: 'CAMERA_UNMUTED' }).effects).toEqual(['replayPreview']);
  });

  it('keeps an interruption message when the session finishes starting', () => {
    const result = next(at('starting', CAMERA_INTERRUPTED_MESSAGE), { type: 'STARTED' });
    expect(result.state).toEqual({ kind: 'running', message: CAMERA_INTERRUPTED_MESSAGE });
  });

  it('stops without reconnecting when another phone takes over', () => {
    const running = next(at('running'), { type: 'TAKEN_OVER' });
    expect(running.state).toEqual({ kind: 'idle', message: TAKEOVER_TEXT });
    expect(running.effects).toEqual(['stopSession']);
    expect(running.ui.startEnabled).toBe(true);

    const starting = next(at('starting'), { type: 'TAKEN_OVER' });
    expect(starting.state.kind).toBe('idle');
    expect(starting.effects).toEqual(['stopSession']);
  });

  it('shows the reason it was given when stopped by the service', () => {
    const busy = next(at('starting'), { type: 'TAKEN_OVER', message: 'Another phone has priority.' });
    expect(busy.state).toEqual({ kind: 'idle', message: 'Another phone has priority.' });
    expect(busy.effects).toEqual(['stopSession']);
  });

  it('does not restart after a takeover when the page becomes visible', () => {
    const taken = next(at('running'), { type: 'TAKEN_OVER' }).state;
    const hidden = next(taken, { type: 'HIDDEN' }).state;
    const visible = next(hidden, { type: 'VISIBLE' });
    expect(visible.state.kind).toBe('idle');
    expect(visible.effects).toEqual([]);
    expect(visible.ui.message).toBe(TAKEOVER_TEXT);
  });

  it('ignores events that do not apply to the current state', () => {
    const ignored: [LifecycleKind, Parameters<typeof next>[1]][] = [
      ['idle', { type: 'STOP' }],
      ['idle', { type: 'HIDDEN' }],
      ['idle', { type: 'CAMERA_ENDED' }],
      ['idle', { type: 'START_FAILED', message: 'late' }],
      ['starting', { type: 'START' }],
      ['starting', { type: 'VISIBLE' }],
      ['running', { type: 'START' }],
      ['running', { type: 'VISIBLE' }],
      ['running', { type: 'START_FAILED', message: 'late' }],
      ['paused', { type: 'START' }],
      ['paused', { type: 'HIDDEN' }],
      ['paused', { type: 'CAMERA_MUTED' }],
      ['paused', { type: 'START_FAILED', message: 'late' }],
    ];
    for (const [kind, event] of ignored) {
      const result = next(at(kind, 'kept'), event);
      expect(result.state, `${kind} + ${event.type}`).toEqual({ kind, message: 'kept' });
      expect(result.effects, `${kind} + ${event.type}`).toEqual([]);
    }
  });

  it('stops a session that reports started in a state that is not waiting for one', () => {
    for (const kind of ['running', 'paused'] as const) {
      const result = next(at(kind), { type: 'STARTED' });
      expect(result.state.kind).toBe(kind);
      expect(result.effects).toEqual(kind === 'paused' ? ['stopSession'] : []);
    }
  });

  it('treats pagehide while paused as a stop', () => {
    const result = next(at('paused'), { type: 'PAGEHIDE' });
    expect(result.state.kind).toBe('idle');
    expect(result.effects).toEqual([]);
  });

  it('keeps Stop enabled and settings locked while paused', () => {
    expect(uiFor(at('paused'))).toMatchObject({ startEnabled: false, stopEnabled: true, settingsEnabled: false });
  });
});
