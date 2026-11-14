// Requests to the service's /api. Every response is an envelope
// {success, data, error}; anything else is reported by its HTTP status.

const HTTP_OK_MIN = 200;
const HTTP_OK_MAX = 299;

interface Envelope {
  readonly success: boolean;
  readonly error: string | null;
}

function asEnvelope(body: unknown): Envelope | null {
  if (typeof body !== 'object' || body === null) return null;
  const candidate = body as { success?: unknown; error?: unknown };
  if (typeof candidate.success !== 'boolean') return null;
  return { success: candidate.success, error: typeof candidate.error === 'string' ? candidate.error : null };
}

const httpLabel = (status: number, statusText: string): string =>
  statusText === '' ? `HTTP ${status}` : `HTTP ${status} ${statusText}`;

/**
 * Null when the request succeeded, otherwise one line for the page: the
 * service's own reason when it gave one, else the HTTP status.
 */
export function requestError(action: string, status: number, statusText: string, body: unknown): string | null {
  const envelope = asEnvelope(body);
  const ok = status >= HTTP_OK_MIN && status <= HTTP_OK_MAX;
  if (ok && envelope?.success === true) return null;
  const reason = envelope?.success === false && envelope.error !== null ? envelope.error : httpLabel(status, statusText);
  return `Could not ${action}: ${reason}`;
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

/** Sends a request and resolves to null on success or to a message saying why it failed. Never rejects. */
export async function sendRequest(
  action: string,
  url: string,
  method: 'POST' | 'DELETE',
  body: unknown,
  fetchFn: typeof fetch = fetch,
): Promise<string | null> {
  const init: RequestInit =
    body === undefined
      ? { method }
      : { method, body: JSON.stringify(body), headers: { 'content-type': 'application/json' } };
  try {
    const response = await fetchFn(url, init);
    return requestError(action, response.status, response.statusText, await readJson(response));
  } catch (error) {
    return `Could not reach the service: ${error instanceof Error ? error.message : 'unknown error'}`;
  }
}
