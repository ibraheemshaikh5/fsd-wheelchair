import { describe, expect, it } from 'vitest';
import { encodeFrame, parseServerMessage, PROTOCOL_VERSION } from '../../shared/protocol';

const JPEG = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 0, 0, 0xff, 0xd9]);

describe('encodeFrame', () => {
  it('prefixes a little-endian header length, then header JSON, then the JPEG', () => {
    const header = { seq: 7, t: 1234.5, w: 640, h: 480 };
    const bytes = encodeFrame(header, JPEG);

    const length = new DataView(bytes.buffer, bytes.byteOffset).getUint32(0, true);
    const headerJson = new TextDecoder().decode(bytes.subarray(4, 4 + length));
    expect(JSON.parse(headerJson)).toEqual(header);
    expect(Array.from(bytes.subarray(4 + length))).toEqual(Array.from(JPEG));
  });

  it('does not modify the JPEG it was given', () => {
    const copy = JPEG.slice();
    encodeFrame({ seq: 0, t: 1, w: 1, h: 1 }, JPEG);
    expect(JPEG).toEqual(copy);
  });
});

describe('parseServerMessage', () => {
  it('reads a pong', () => {
    const raw = JSON.stringify({ v: 1, type: 'pong', t: 10, server_t: 20 });
    expect(parseServerMessage(raw)).toEqual({ v: 1, type: 'pong', t: 10, server_t: 20 });
  });

  it('reads an error', () => {
    const raw = JSON.stringify({ v: 1, type: 'error', message: 'lat: too big' });
    expect(parseServerMessage(raw)).toEqual({ v: 1, type: 'error', message: 'lat: too big' });
  });

  it('reads an ack', () => {
    const raw = JSON.stringify({ v: 1, type: 'ack', seq: 42, t: 1_700_000_000_000.5 });
    expect(parseServerMessage(raw)).toEqual({ v: 1, type: 'ack', seq: 42, t: 1_700_000_000_000.5 });
  });

  it.each([
    ['ack without seq', JSON.stringify({ v: 1, type: 'ack', t: 1 })],
    ['ack with a fractional seq', JSON.stringify({ v: 1, type: 'ack', seq: 1.5, t: 1 })],
    ['ack with a negative seq', JSON.stringify({ v: 1, type: 'ack', seq: -1, t: 1 })],
    ['ack without a time', JSON.stringify({ v: 1, type: 'ack', seq: 1 })],
    ['not json', '{nope'],
    ['wrong version', JSON.stringify({ v: 2, type: 'pong', t: 1, server_t: 2 })],
    ['unknown type', JSON.stringify({ v: 1, type: 'hug' })],
    ['pong missing server time', JSON.stringify({ v: 1, type: 'pong', t: 1 })],
    ['error without message', JSON.stringify({ v: 1, type: 'error' })],
    ['an array', '[]'],
    ['null', 'null'],
  ])('rejects %s', (_label, raw) => {
    expect(parseServerMessage(raw)).toBeNull();
  });

  it('exposes the protocol version', () => {
    expect(PROTOCOL_VERSION).toBe(1);
  });
});
