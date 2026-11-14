import { backoffDelayMs } from '../../shared/backoff';
import { beforeEach, describe, expect, it } from 'vitest';
import {
  CLOSE_BUSY,
  CLOSE_IDLE,
  CLOSE_NO_REPLY,
  CLOSE_REPLACED,
  NO_REPLY_REASON,
  SOCKET_OPEN,
  type SocketLike,
  Transport,
  type TransportState,
} from '../../capture/transport';

class FakeSocket implements SocketLike {
  readyState = 0;
  bufferedAmount = 0;
  binaryType: BinaryType = 'blob';
  sent: (string | Uint8Array<ArrayBuffer>)[] = [];
  closed = false;
  onopen: (() => void) | null = null;
  onclose: ((event: { code: number; reason: string }) => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(readonly url: string) {}

  send(data: string | Uint8Array<ArrayBuffer>): void {
    this.sent.push(data);
  }

  close(): void {
    this.closed = true;
  }

  open(): void {
    this.readyState = SOCKET_OPEN;
    this.onopen?.();
  }

  drop(code = 1006, reason = ''): void {
    this.readyState = 3;
    this.onclose?.({ code, reason });
  }
}

describe('backoffDelayMs', () => {
  it('doubles from the base and caps at the maximum', () => {
    expect([0, 1, 2, 3, 10].map((attempt) => backoffDelayMs(attempt, 500, 4000))).toEqual([
      500, 1000, 2000, 4000, 4000,
    ]);
  });
});

describe('Transport', () => {
  let sockets: FakeSocket[];
  let timers: { fn: () => void; ms: number }[];
  let states: TransportState[];
  let messages: unknown[];
  let transport: Transport;
  let now: number;

  beforeEach(() => {
    now = 10_000;
    sockets = [];
    timers = [];
    states = [];
    messages = [];
    transport = new Transport('wss://chair/ws/phone', {
      createSocket: (url) => {
        const socket = new FakeSocket(url);
        sockets.push(socket);
        return socket;
      },
      setTimer: (fn, ms) => {
        timers.push({ fn, ms });
        return timers.length;
      },
      clearTimer: () => {},
      onState: (state) => states.push(state),
      onMessage: (message) => messages.push(message),
      sendBudget: { budgetMs: 100, maxBytes: 1000 },
      now: () => now,
    });
  });

  it('connects in binary mode and reports state changes', () => {
    transport.start();
    expect(sockets[0]?.binaryType).toBe('arraybuffer');
    expect(states.at(-1)).toEqual({ status: 'connecting', attempt: 0, lastClose: null });
    sockets[0]?.open();
    expect(states.at(-1)).toEqual({ status: 'open', attempt: 0, lastClose: null });
  });

  it('only sends while open', () => {
    transport.start();
    expect(transport.sendJson({ hi: 1 })).toBe(false);
    sockets[0]?.open();
    expect(transport.sendJson({ hi: 1 })).toBe(true);
    expect(sockets[0]?.sent).toEqual(['{"hi":1}']);
  });

  it('skips frames when the socket is backed up', () => {
    transport.start();
    sockets[0]?.open();
    const socket = sockets[0] as FakeSocket;
    expect(transport.sendFrame(new Uint8Array([1]))).toBe(true);
    socket.bufferedAmount = 5000;
    expect(transport.sendFrame(new Uint8Array([2]))).toBe(false);
    expect(socket.sent).toHaveLength(1);
  });

  it('reconnects with growing delays and resets once connected', () => {
    transport.start();
    sockets[0]?.drop(1006);
    expect(timers.at(-1)?.ms).toBe(500);
    expect(states.at(-1)).toEqual({ status: 'waiting', attempt: 1, lastClose: { code: 1006, reason: '' } });

    timers.at(-1)?.fn();
    sockets[1]?.drop();
    expect(timers.at(-1)?.ms).toBe(1000);

    timers.at(-1)?.fn();
    sockets[2]?.open();
    sockets[2]?.onmessage?.({ data: JSON.stringify({ v: 1, type: 'pong', t: 1, server_t: 2 }) });
    sockets[2]?.drop();
    expect(timers.at(-1)?.ms).toBe(500);
  });

  it('keeps backing off when the service closes right after accepting', () => {
    transport.start();
    for (let i = 0; i < 3; i += 1) {
      sockets[i]?.open();
      sockets[i]?.drop(CLOSE_IDLE, 'no data for 5 s');
      timers.at(-1)?.fn();
    }
    expect(timers.map((timer) => timer.ms)).toEqual([500, 1000, 2000]);
  });

  it('resets the backoff once the socket has stayed open for a while', () => {
    transport.start();
    sockets[0]?.drop();
    timers.at(-1)?.fn();
    sockets[1]?.drop();
    timers.at(-1)?.fn();
    sockets[2]?.open();
    now += 2000;
    sockets[2]?.drop(CLOSE_IDLE);
    expect(timers.at(-1)?.ms).toBe(500);
    expect(states.at(-1)).toMatchObject({ status: 'waiting', attempt: 1 });
  });

  it('stops for good without reconnecting when a newer phone replaces this one', () => {
    transport.start();
    sockets[0]?.open();
    sockets[0]?.drop(CLOSE_REPLACED, 'replaced by a newer phone connection');
    expect(timers).toHaveLength(0);
    expect(states.at(-1)).toEqual({
      status: 'stopped',
      attempt: 0,
      lastClose: { code: CLOSE_REPLACED, reason: 'replaced by a newer phone connection' },
    });
    transport.start();
    expect(sockets).toHaveLength(2);
  });

  it('stops for good without reconnecting when a priority phone holds the slot', () => {
    transport.start();
    sockets[0]?.open();
    sockets[0]?.drop(CLOSE_BUSY, 'another phone has priority');
    expect(timers).toHaveLength(0);
    expect(states.at(-1)).toEqual({
      status: 'stopped',
      attempt: 0,
      lastClose: { code: CLOSE_BUSY, reason: 'another phone has priority' },
    });
  });

  it('reconnects when the service has said nothing for too long', () => {
    transport.start();
    sockets[0]?.open();
    const opened = now;
    transport.checkLiveness(opened + 6000, 5000);
    expect(sockets[0]?.closed).toBe(true);
    expect(states.at(-1)?.status).toBe('waiting');
    expect(states.at(-1)?.lastClose).toEqual({ code: CLOSE_NO_REPLY, reason: NO_REPLY_REASON });
    // The late close event from the abandoned socket changes nothing.
    sockets[0]?.drop();
    expect(timers).toHaveLength(1);
    timers.at(-1)?.fn();
    expect(sockets).toHaveLength(2);
  });

  it('stays connected while the service keeps answering', () => {
    transport.start();
    sockets[0]?.open();
    const opened = now;
    now = opened + 4000;
    sockets[0]?.onmessage?.({ data: new ArrayBuffer(1) });
    transport.checkLiveness(opened + 6000, 5000);
    expect(states.at(-1)?.status).toBe('open');
    expect(sockets[0]?.closed).toBe(false);
  });

  it('does not check liveness unless open', () => {
    transport.checkLiveness(now + 60_000, 5000);
    transport.start();
    transport.checkLiveness(now + 60_000, 5000);
    expect(states.at(-1)?.status).toBe('connecting');
  });

  it('drops droppable messages under backpressure but never the others', () => {
    transport.start();
    sockets[0]?.open();
    const socket = sockets[0] as FakeSocket;
    socket.bufferedAmount = 200_000;
    expect(transport.sendJson({ type: 'heading' }, true)).toBe(false);
    expect(socket.sent).toHaveLength(0);
    expect(transport.sendJson({ type: 'ping' })).toBe(true);
    expect(socket.sent).toEqual(['{"type":"ping"}']);
  });

  it('passes parsed server messages on and ignores junk', () => {
    transport.start();
    sockets[0]?.open();
    sockets[0]?.onmessage?.({ data: JSON.stringify({ v: 1, type: 'pong', t: 1, server_t: 2 }) });
    sockets[0]?.onmessage?.({ data: 'junk' });
    sockets[0]?.onmessage?.({ data: new ArrayBuffer(2) });
    expect(messages).toEqual([{ v: 1, type: 'pong', t: 1, server_t: 2 }]);
  });

  it('stops for good when asked', () => {
    transport.start();
    sockets[0]?.open();
    transport.stop();
    expect(sockets[0]?.closed).toBe(true);
    sockets[0]?.drop();
    expect(timers).toHaveLength(0);
    expect(states.at(-1)?.status).toBe('stopped');
    expect(transport.sendJson({})).toBe(false);
  });

  it('ignores a second start while running', () => {
    transport.start();
    transport.start();
    expect(sockets).toHaveLength(1);
  });

  it('ignores events from a socket it has replaced', () => {
    transport.start();
    const old = sockets[0] as FakeSocket;
    old.drop();
    timers.at(-1)?.fn();
    old.onopen?.();
    expect(states.at(-1)?.status).toBe('connecting');
  });
});
