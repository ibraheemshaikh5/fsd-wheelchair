// Wire protocol v1 between the phone and the driving service.
// Mirrors service/drive_service/protocol.py; the contract is in docs/phone-protocol.md.

export const PROTOCOL_VERSION = 1 as const;

export interface FrameHeader {
  /** Counts every captured frame, including ones skipped before sending. */
  readonly seq: number;
  /** Capture time, phone clock, ms since the Unix epoch. */
  readonly t: number;
  readonly w: number;
  readonly h: number;
  /** Clockwise degrees the page turned the image (upright, landscape when the phone is sideways). */
  readonly rot?: 0 | 90 | 180 | 270;
}

export interface CameraInfo {
  readonly label: string;
  readonly width: number;
  readonly height: number;
  readonly fps: number;
}

export interface MountInfo {
  readonly height_m: number;
  readonly pitch_deg: number;
}

interface PhoneMessageBase {
  readonly v: typeof PROTOCOL_VERSION;
  /** Phone clock, ms since the Unix epoch. */
  readonly t: number;
}

export interface HelloMessage extends PhoneMessageBase {
  readonly type: 'hello';
  readonly user_agent: string;
  readonly camera: CameraInfo;
  readonly mount: MountInfo | null;
  /** 'device_xyz': IMU rotation columns are about device x, y, z. Absent in older recordings. */
  readonly imu_axes?: 'device_xyz';
}

export interface GpsMessage extends PhoneMessageBase {
  readonly type: 'gps';
  readonly lat: number;
  readonly lon: number;
  readonly accuracy_m: number;
  readonly altitude_m: number | null;
  readonly speed_mps: number | null;
  /** Direction of travel, degrees clockwise from true north. */
  readonly course_deg: number | null;
  readonly fix_t: number;
}

export interface HeadingMessage extends PhoneMessageBase {
  readonly type: 'heading';
  /** Degrees clockwise from magnetic north. */
  readonly magnetic_deg: number;
  readonly accuracy_deg: number | null;
}

/** [ms since message t, accel x, y, z (m/s², with gravity), rotation rate x, y, z (deg/s)] */
export type ImuSample = readonly [number, number, number, number, number, number, number];

export interface ImuMessage extends PhoneMessageBase {
  readonly type: 'imu';
  readonly samples: readonly ImuSample[];
}

export interface PingMessage extends PhoneMessageBase {
  readonly type: 'ping';
  /** The phone's measurement from its previous ping/pong exchange. */
  readonly rtt_ms: number | null;
  readonly offset_ms: number | null;
}

/**
 * Once a second, how the frame stream is coping with the link. Counters run
 * from the start of this connection and reset on reconnect.
 */
export interface LinkMessage extends PhoneMessageBase {
  readonly type: 'link';
  /** Frames the camera delivered while this connection was open. */
  readonly captured: number;
  readonly sent: number;
  /** Acks received. */
  readonly acked: number;
  /** Held back because too many frames were waiting for their ack. */
  readonly skipped_flow: number;
  /** Encoder busy or failed, or a newer frame went out first. */
  readonly skipped_encoder: number;
  /** Held back by the socket's send budget (bufferedAmount). */
  readonly skipped_socket: number;
  readonly in_flight: number;
  /** JPEG quality in use now (adapts to the link). */
  readonly jpeg_quality: number;
  /** Smoothed frame send-to-ack round trip, null before the first ack. */
  readonly ack_rtt_ms: number | null;
}

export type PhoneMessage = HelloMessage | GpsMessage | HeadingMessage | ImuMessage | PingMessage | LinkMessage;

export interface PongMessage {
  readonly v: typeof PROTOCOL_VERSION;
  readonly type: 'pong';
  /** The ping's t, echoed back. */
  readonly t: number;
  /** Service clock when it answered, ms since the Unix epoch. */
  readonly server_t: number;
}

export interface ErrorMessage {
  readonly v: typeof PROTOCOL_VERSION;
  readonly type: 'error';
  readonly message: string;
}

/** Sent after each binary frame the service handled, in order: the phone's flow control. */
export interface AckMessage {
  readonly v: typeof PROTOCOL_VERSION;
  readonly type: 'ack';
  /** The frame's seq. */
  readonly seq: number;
  /** Service clock when it handled the frame, ms since the Unix epoch. */
  readonly t: number;
}

export type ServerMessage = PongMessage | ErrorMessage | AckMessage;

const HEADER_LENGTH_BYTES = 4;

/** Build a binary frame: [uint32 LE header length][header JSON][JPEG bytes]. */
export function encodeFrame(header: FrameHeader, jpeg: Uint8Array): Uint8Array<ArrayBuffer> {
  const headerBytes = new TextEncoder().encode(JSON.stringify(header));
  const out = new Uint8Array(HEADER_LENGTH_BYTES + headerBytes.length + jpeg.length);
  new DataView(out.buffer).setUint32(0, headerBytes.length, true);
  out.set(headerBytes, HEADER_LENGTH_BYTES);
  out.set(jpeg, HEADER_LENGTH_BYTES + headerBytes.length);
  return out;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

export const isFiniteNumber = (value: unknown): value is number =>
  typeof value === 'number' && Number.isFinite(value);

/** Validate a text message from the service. Returns null for anything unexpected. */
export function parseServerMessage(raw: string): ServerMessage | null {
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!isRecord(data) || data.v !== PROTOCOL_VERSION) return null;
  if (data.type === 'pong' && isFiniteNumber(data.t) && isFiniteNumber(data.server_t)) {
    return { v: PROTOCOL_VERSION, type: 'pong', t: data.t, server_t: data.server_t };
  }
  if (data.type === 'ack' && Number.isSafeInteger(data.seq) && (data.seq as number) >= 0 && isFiniteNumber(data.t)) {
    return { v: PROTOCOL_VERSION, type: 'ack', seq: data.seq as number, t: data.t };
  }
  if (data.type === 'error' && typeof data.message === 'string') {
    return { v: PROTOCOL_VERSION, type: 'error', message: data.message };
  }
  return null;
}
