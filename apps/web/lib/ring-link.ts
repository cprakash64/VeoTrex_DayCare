export const RING_NONCE_PATTERN = /^[A-Za-z0-9_-]{43}$/;

export function validRingLinkParameters(nonce: unknown, time: unknown): boolean {
  if (typeof nonce !== "string" || !RING_NONCE_PATTERN.test(nonce)) return false;
  if (typeof time !== "string" || !/^\d{1,16}$/.test(time)) return false;
  const value = Number(time);
  return Number.isSafeInteger(value) && value >= 0;
}

export function isSameOriginPost(requestUrl: string, origin: string | null): boolean {
  if (!origin) return false;
  return new URL(requestUrl).origin === origin;
}
