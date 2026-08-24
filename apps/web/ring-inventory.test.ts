import { describe, expect, it } from "vitest";

import { providerStatus, UUID_PATTERN } from "./lib/ring-inventory";

describe("Ring inventory presentation boundary", () => {
  it("shows provider status without implying stream or AI health", () => {
    expect(providerStatus(true)).toBe("Ring online");
    expect(providerStatus(false)).toBe("Ring offline");
    expect(providerStatus(null)).toBe("Ring status unknown");
  });

  it("accepts only internal UUID connection identifiers for the sync BFF", () => {
    expect(UUID_PATTERN.test("de305d54-75b4-431b-adb2-eb6b9e546014")).toBe(true);
    expect(UUID_PATTERN.test("../other-tenant")).toBe(false);
  });
});
