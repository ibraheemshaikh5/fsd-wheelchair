import { describe, expect, it } from 'vitest';
import { requestError, sendRequest } from '../../shared/api';

describe('requestError', () => {
  it('returns null for a successful envelope', () => {
    expect(requestError('start recording', 200, '', { success: true, data: {}, error: null })).toBeNull();
  });

  it("shows the server's 409 message", () => {
    const body = { success: false, data: null, error: 'phone is not connected' };
    expect(requestError('start recording', 409, 'Conflict', body)).toBe(
      'Could not start recording: phone is not connected',
    );
  });

  it("shows the server's 507 message", () => {
    const body = { success: false, data: null, error: 'not enough free disk (1.2 GB free, need 2 GB)' };
    expect(requestError('start recording', 507, 'Insufficient Storage', body)).toBe(
      'Could not start recording: not enough free disk (1.2 GB free, need 2 GB)',
    );
  });

  it('falls back to the HTTP status when the body is not an envelope', () => {
    expect(requestError('stop recording', 502, 'Bad Gateway', null)).toBe('Could not stop recording: HTTP 502 Bad Gateway');
    expect(requestError('stop recording', 500, '', 'oops')).toBe('Could not stop recording: HTTP 500');
    expect(requestError('start recording', 409, 'Conflict', { success: false, error: 42 })).toBe(
      'Could not start recording: HTTP 409 Conflict',
    );
  });

  it('treats a 2xx with success false as a failure', () => {
    expect(requestError('start recording', 200, 'OK', { success: false, data: null, error: 'busy' })).toBe(
      'Could not start recording: busy',
    );
  });

  it('treats a non-2xx with a malformed success flag as a failure', () => {
    expect(requestError('start recording', 409, 'Conflict', { success: true })).toBe(
      'Could not start recording: HTTP 409 Conflict',
    );
  });
});

describe('sendRequest', () => {
  const respond = (status: number, body: unknown, statusText = ''): typeof fetch =>
    (async () => new Response(JSON.stringify(body), { status, statusText })) as typeof fetch;

  it('sends JSON and is null on success', async () => {
    const calls: [string, RequestInit | undefined][] = [];
    const fetchFn = (async (url: string, init?: RequestInit) => {
      calls.push([url, init]);
      return new Response(JSON.stringify({ success: true, data: {}, error: null }));
    }) as typeof fetch;
    await expect(sendRequest('set destination', '/api/destination', 'POST', { lat: 1, lon: 2 }, fetchFn)).resolves.toBeNull();
    expect(calls[0]?.[0]).toBe('/api/destination');
    expect(calls[0]?.[1]?.method).toBe('POST');
    expect(calls[0]?.[1]?.body).toBe('{"lat":1,"lon":2}');
    expect(new Headers(calls[0]?.[1]?.headers).get('content-type')).toBe('application/json');
  });

  it('sends no body without one', async () => {
    let init: RequestInit | undefined;
    const fetchFn = (async (_: string, i?: RequestInit) => {
      init = i;
      return new Response(JSON.stringify({ success: true, data: null, error: null }));
    }) as typeof fetch;
    await sendRequest('stop recording', '/api/recording/stop', 'POST', undefined, fetchFn);
    expect(init?.body).toBeUndefined();
  });

  it("gives the service's reason, or the HTTP status for a non-JSON page", async () => {
    await expect(
      sendRequest('start recording', '/x', 'POST', undefined, respond(409, { success: false, data: null, error: 'busy' })),
    ).resolves.toBe('Could not start recording: busy');
    const html = (async () => new Response('<html>', { status: 502, statusText: 'Bad Gateway' })) as typeof fetch;
    await expect(sendRequest('clear destination', '/x', 'DELETE', undefined, html)).resolves.toBe(
      'Could not clear destination: HTTP 502 Bad Gateway',
    );
  });

  it('says when the service cannot be reached', async () => {
    const down = (async () => {
      throw new TypeError('Load failed');
    }) as typeof fetch;
    await expect(sendRequest('set destination', '/x', 'POST', {}, down)).resolves.toBe(
      'Could not reach the service: Load failed',
    );
    const odd = (async () => {
      throw 'nope';
    }) as typeof fetch;
    await expect(sendRequest('set destination', '/x', 'POST', {}, odd)).resolves.toBe(
      'Could not reach the service: unknown error',
    );
  });
});
