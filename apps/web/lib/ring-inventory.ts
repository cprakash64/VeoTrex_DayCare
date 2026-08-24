export const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function providerStatus(value: boolean | null): string {
  return value === true ? "Ring online" : value === false ? "Ring offline" : "Ring status unknown";
}
