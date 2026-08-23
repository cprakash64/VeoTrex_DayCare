import { describe, expect, it } from "vitest";

import { isSameOriginPost, validRingLinkParameters } from "./lib/ring-link";

describe("Ring link browser boundary", () => {
  it("accepts only bounded timestamp and URL-safe unpadded nonce shapes", () => {
    expect(validRingLinkParameters("A".repeat(43), "1750000000000")).toBe(true);
    expect(validRingLinkParameters("+".repeat(43), "1750000000000")).toBe(false);
    expect(validRingLinkParameters("A".repeat(43), "1e9")).toBe(false);
  });

  it("requires a same-origin POST", () => {
    expect(isSameOriginPost("https://app.example/api/claim", "https://app.example")).toBe(true);
    expect(isSameOriginPost("https://app.example/api/claim", "https://evil.example")).toBe(false);
    expect(isSameOriginPost("https://app.example/api/claim", null)).toBe(false);
  });
});
