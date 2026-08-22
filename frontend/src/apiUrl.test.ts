import { describe, expect, it } from "vitest";

import { resolveApiUrl } from "./apiUrl";

describe("resolveApiUrl", () => {
  it("makes same-origin API paths absolute for MapLibre workers", () => {
    expect(resolveApiUrl(
      "/api/tiles/release-1/{z}/{x}/{y}.pbf",
      "",
      "http://192.168.1.6:54711",
    )).toBe("http://192.168.1.6:54711/api/tiles/release-1/{z}/{x}/{y}.pbf");
  });

  it("preserves a configured API base path", () => {
    expect(resolveApiUrl("/api/health", "/sanitation", "https://maps.example.test"))
      .toBe("https://maps.example.test/sanitation/api/health");
  });

  it("supports an absolute configured API base", () => {
    expect(resolveApiUrl("/api/health", "https://api.example.test/v1/", "https://maps.example.test"))
      .toBe("https://api.example.test/v1/api/health");
  });

  it("leaves an absolute backend URL unchanged", () => {
    expect(resolveApiUrl("https://tiles.example.test/1/2/3.pbf", "", "https://maps.example.test"))
      .toBe("https://tiles.example.test/1/2/3.pbf");
  });
});
