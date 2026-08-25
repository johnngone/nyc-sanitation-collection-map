import { describe, expect, it } from "vitest";

import {
  locationStatePresentation,
  normalizeHeading,
  orientationHeading,
  preferredHeading,
  validHeading,
} from "./userLocation";

describe("user location heading", () => {
  it("normalizes headings to a clockwise 0-360 range", () => {
    expect(normalizeHeading(360)).toBe(0);
    expect(normalizeHeading(-20)).toBe(340);
    expect(normalizeHeading(725)).toBe(5);
    expect(validHeading(Number.NaN)).toBeUndefined();
    expect(validHeading(null)).toBeUndefined();
  });

  it("prefers an iOS compass heading when it is valid", () => {
    expect(orientationHeading({
      absolute: false,
      alpha: null,
      webkitCompassHeading: 32,
      webkitCompassAccuracy: 8,
    })).toBe(32);
    expect(orientationHeading({
      absolute: false,
      alpha: null,
      webkitCompassHeading: 32,
      webkitCompassAccuracy: -1,
    })).toBeUndefined();
  });

  it("converts absolute alpha and accounts for screen rotation", () => {
    expect(orientationHeading({ absolute: true, alpha: 30 }, 0)).toBe(330);
    expect(orientationHeading({ absolute: true, alpha: 30 }, 90)).toBe(60);
    expect(orientationHeading({ absolute: false, alpha: 30 }, 0)).toBeUndefined();
  });

  it("uses movement heading only when a compass heading is unavailable", () => {
    expect(preferredHeading(20, 80)).toBe(20);
    expect(preferredHeading(undefined, 80)).toBe(80);
    expect(preferredHeading(undefined, null)).toBeUndefined();
  });
});

describe("user location presentation", () => {
  it("exposes accessible labels and tracking state", () => {
    expect(locationStatePresentation("inactive")).toEqual({ label: "Show your location", pressed: false });
    expect(locationStatePresentation("requesting").pressed).toBe(true);
    expect(locationStatePresentation("active")).toEqual({ label: "Center map on your location", pressed: true });
    expect(locationStatePresentation("stale").label).toContain("last known");
    expect(locationStatePresentation("denied").pressed).toBe(false);
  });
});
