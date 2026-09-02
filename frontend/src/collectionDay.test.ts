import { describe, expect, it } from "vitest";

import { automaticCollectionDay, dayFromCode } from "./collectionDay";

describe("automatic collection day", () => {
  it.each([
    ["2026-08-31T19:59:59Z", "Monday"],
    ["2026-08-31T20:00:00Z", "Tuesday"],
  ])("uses the 4 PM New York cutoff during daylight time at %s", (instant, expected) => {
    expect(automaticCollectionDay(new Date(instant))).toBe(expected);
  });

  it.each([
    ["2026-01-05T20:59:59Z", "Monday"],
    ["2026-01-05T21:00:00Z", "Tuesday"],
  ])("uses the 4 PM New York cutoff during standard time at %s", (instant, expected) => {
    expect(automaticCollectionDay(new Date(instant))).toBe(expected);
  });

  it("rolls Friday evening forward to Saturday", () => {
    expect(automaticCollectionDay(new Date("2026-09-04T20:00:00Z"))).toBe("Saturday");
  });

  it.each([
    ["2026-09-05T19:59:59Z", "Saturday"],
    ["2026-09-05T20:00:00Z", "Monday"],
  ])("skips Sunday at the Saturday cutoff at %s", (instant, expected) => {
    expect(automaticCollectionDay(new Date(instant))).toBe(expected);
  });

  it.each([
    "2026-09-06T04:00:00Z",
    "2026-09-06T19:59:59Z",
    "2026-09-06T20:00:00Z",
  ])("selects Monday throughout Sunday at %s", (instant) => {
    expect(automaticCollectionDay(new Date(instant))).toBe("Monday");
  });
});

describe("initial collection day", () => {
  const mondayAfterCutoff = new Date("2026-08-31T20:00:00Z");

  it("preserves a valid query-string day as an explicit override", () => {
    expect(dayFromCode("MON", mondayAfterCutoff)).toBe("Monday");
  });

  it.each([null, "SUN", "invalid"])("uses the automatic day for %s", (code) => {
    expect(dayFromCode(code, mondayAfterCutoff)).toBe("Tuesday");
  });
});
