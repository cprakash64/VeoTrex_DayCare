import { describe, expect, it } from "vitest";

import { isSameOriginPost, validRingLinkParameters } from "./lib/ring-link";

describe("Ring link browser boundary", () => {
  it("accepts only bounded timestamp and URL-safe unpadded nonce shapes", () => {
    expect(validRingLinkParameters("A".repeat(43), "1750000000000")).toBe(true);
    expect(validRingLinkParameters("+".repeat(43), "1750000000000")).toBe(false);
    expect(validRingLinkParameters("A".repeat(43), "1e9")).toBe(false);
  });

  it("requires the browser origin to match the configured public app origin", () => {
    expect(isSameOriginPost("https://app.example", "https://app.example")).toBe(true);
    expect(isSameOriginPost("https://app.example/", "https://app.example")).toBe(true);
    expect(isSameOriginPost("https://app.example/path", "https://app.example")).toBe(true);

    expect(isSameOriginPost("https://app.example", "https://evil.example")).toBe(false);
    expect(isSameOriginPost("http://app.example", "https://app.example")).toBe(false);

    expect(isSameOriginPost(undefined, "https://app.example")).toBe(false);
    expect(isSameOriginPost("", "https://app.example")).toBe(false);
    expect(isSameOriginPost("not-a-url", "https://app.example")).toBe(false);
    expect(isSameOriginPost("https://app.example", null)).toBe(false);
    expect(isSameOriginPost("https://app.example", "not-a-url")).toBe(false);
  });
});
