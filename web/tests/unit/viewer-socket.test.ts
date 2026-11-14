import { describe, expect, it } from 'vitest';
import {
  CLOSE_TOO_MANY_VIEWERS,
  connectViewer,
  type ViewerConnection,
  type ViewerSocket,
  viewerSocketUrl,
} from '../../shared/viewer-socket';
import type { ServiceStatus } from '../../shared/status';

class FakeSocket implements ViewerSocket {
  binaryType: BinaryType = 'blob';
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  closed = false;
  constructor(readonly url: string) {}
  open(): void {
    this.onopen?.({} as Event);
  }
  receive(data: unknown): void {
    this.onmessage?.({ data } as MessageEvent);
  }
  drop(code = 1006): void {
    this.onclose?.({ code } as CloseEvent);
  }
  close(): void {
    this.closed = true;
  }
}

const STATUS = JSON.stringify({ type: 'status', phone_connected: true, stats: {}, recording: {} });

function setup() {
  const sockets: FakeSocket[] = [];
  const timers = new Map<number, { fn: () => void; ms: number }>();
  let nextTimer = 1;
  const connections: ViewerConnection[] = [];
  const statuses: ServiceStatus[] = [];
  const frames: ArrayBuffer[] = [];
  const handle = connectViewer({
    url: 'ws://host/ws/viewer',
    createSocket: (url) => {
      const socket = new FakeSocket(url);
      sockets.push(socket);
      return socket;
    },
    setTimer: (fn, ms) => {
      const id = nextTimer++;
      timers.set(id, { fn, ms });
      return id;
    },
    clearTimer: (id) => timers.delete(id),
    onConnection: (state) => connections.push(state),
    onStatus: (status) => statuses.push(status),
    onFrame: (jpeg) => frames.push(jpeg),
  });
  /** Fires the most recent pending timer and returns its delay. */
  const fireTimer = (): number => {
    const [id, timer] = [...timers.entries()].at(-1) ?? [];
    if (id === undefined || timer === undefined) throw new Error('no timer pending');
    timers.delete(id);
    timer.fn();
    return timer.ms;
  };
  return { handle, sockets, timers, connections, statuses, frames, fireTimer };
}

describe('viewerSocketUrl', () => {
  it('uses wss on https and ws otherwise', () => {
    expect(viewerSocketUrl({ protocol: 'https:', host: 'mac.ts.net' })).toBe('wss://mac.ts.net/ws/viewer');
    expect(viewerSocketUrl({ protocol: 'http:', host: 'localhost:5173' })).toBe('ws://localhost:5173/ws/viewer');
  });
});

describe('connectViewer', () => {
  it('opens a binary socket and delivers statuses and frames', () => {
    const t = setup();
    const socket = t.sockets[0]!;
    expect(socket.url).toBe('ws://host/ws/viewer');
    expect(socket.binaryType).toBe('arraybuffer');
    socket.open();
    socket.receive(STATUS);
    socket.receive('{"type":"pong"}');
    socket.receive(new ArrayBuffer(4));
    expect(t.connections).toEqual([{ kind: 'open' }]);
    expect(t.statuses).toHaveLength(1);
    expect(t.frames).toHaveLength(1);
  });

  it('backs off from 0.5 s, doubling to 5 s, while the service sends nothing', () => {
    const t = setup();
    const delays: number[] = [];
    for (let i = 0; i < 6; i++) {
      t.sockets.at(-1)!.drop();
      delays.push(t.fireTimer());
    }
    expect(delays).toEqual([500, 1000, 2000, 4000, 5000, 5000]);
  });

  it('does not reset the backoff on open alone (accepted then closed with 4429)', () => {
    const t = setup();
    t.sockets[0]!.drop();
    t.fireTimer();
    const second = t.sockets[1]!;
    second.open();
    second.drop(CLOSE_TOO_MANY_VIEWERS);
    expect(t.connections.at(-1)).toEqual({ kind: 'lost', reason: 'too many viewers open' });
    expect(t.fireTimer()).toBe(1000);
  });

  it('resets the backoff once the service sends a message', () => {
    const t = setup();
    t.sockets[0]!.drop();
    t.fireTimer();
    const second = t.sockets[1]!;
    second.receive(STATUS);
    second.drop();
    expect(t.connections.at(-1)).toEqual({ kind: 'lost', reason: 'connection lost' });
    expect(t.fireTimer()).toBe(500);
  });

  it('stops reconnecting once closed', () => {
    const t = setup();
    t.sockets[0]!.drop();
    expect(t.timers.size).toBe(1);
    t.handle.close();
    expect(t.timers.size).toBe(0);
    expect(t.sockets).toHaveLength(1);
  });

  it('closes the open socket and ignores its late close event', () => {
    const t = setup();
    const socket = t.sockets[0]!;
    t.handle.close();
    expect(socket.closed).toBe(true);
    socket.drop();
    expect(t.connections).toEqual([]);
    expect(t.timers.size).toBe(0);
  });
});
