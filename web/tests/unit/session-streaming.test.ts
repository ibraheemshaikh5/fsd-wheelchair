import { beforeEach, describe, expect, it } from 'vitest';
import type { PhoneMessage } from '../../shared/protocol';
import { harness, startFully, OPEN, type Harness } from './session-harness';

describe('Session streaming', () => {
  let h: Harness;
  beforeEach(async () => {
    h = harness();
    await startFully(h);
  });

  it('sends hello and then a ping as soon as the socket opens', () => {
    h.transport().state(OPEN);
    expect(h.transport().sentTypes()).toEqual(['hello', 'ping']);
    const hello = h.transport().sendJson.mock.calls[0]?.[0] as PhoneMessage;
    expect(hello).toMatchObject({ type: 'hello', user_agent: 'test-agent', camera: { width: 1280, height: 720 } });
  });

  it('pings again on every reconnect, with the last clock sample', () => {
    h.transport().state(OPEN);
    h.transport().handlers.onMessage({ v: 1, type: 'pong', t: 1_000_000 - 40, server_t: 1_000_000 });
    h.transport().state({ status: 'waiting', attempt: 1, lastClose: { code: 1006, reason: '' } });
    h.transport().sendJson.mockClear();
    h.transport().state(OPEN);
    expect(h.transport().sentTypes()).toEqual(['hello', 'ping']);
    expect(h.transport().sendJson.mock.calls[1]?.[0]).toMatchObject({ type: 'ping', rtt_ms: 40 });
  });
});
