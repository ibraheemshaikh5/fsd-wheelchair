/** The driving service refused a request; `message` is written for the rider. */
export class ServiceRefusedError extends Error {
  override name = 'ServiceRefusedError'
}
