import { describe, expect, it } from "vitest";

import {
  followCameraTransitionDuration,
  headingExceedsDeadband,
  locationStatePresentation,
  locationIsWithinBounds,
  nextCameraFollowState,
  normalizeHeading,
  orientationHeading,
  permissionAllowsAutoStart,
  preferredHeading,
  shouldCenterFirstFix,
  shortestCameraBearing,
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

  it("takes the shortest camera path across north", () => {
    expect(shortestCameraBearing(350, 10)).toBe(370);
    expect(shortestCameraBearing(10, 350)).toBe(-10);
    expect(shortestCameraBearing(-179, 179)).toBe(-181);
  });

  it("coalesces heading jitter inside a three-degree deadband", () => {
    expect(headingExceedsDeadband(undefined, 20)).toBe(true);
    expect(headingExceedsDeadband(359, 1)).toBe(false);
    expect(headingExceedsDeadband(358, 2)).toBe(true);
  });

  it("uses shorter follow transitions while honoring reduced motion", () => {
    expect(followCameraTransitionDuration(false)).toBe(250);
    expect(followCameraTransitionDuration(true)).toBe(0);
  });
});

describe("user location presentation", () => {
  it("exposes accessible labels and tracking state", () => {
    expect(locationStatePresentation("inactive")).toEqual({ label: "Show and follow your location", pressed: false });
    expect(locationStatePresentation("requesting").pressed).toBe(false);
    expect(locationStatePresentation("active")).toEqual({
      label: "Center and follow your location and heading",
      pressed: false,
    });
    expect(locationStatePresentation("active", "following")).toEqual({
      label: "Following your location and heading",
      pressed: true,
    });
    expect(locationStatePresentation("stale").label).toContain("last known");
    expect(locationStatePresentation("denied").pressed).toBe(false);
  });

  it("enters follow on activation and pauses after user navigation or reset", () => {
    expect(nextCameraFollowState("free", "activate")).toBe("following");
    expect(nextCameraFollowState("following", "activate")).toBe("following");
    expect(nextCameraFollowState("following", "pause")).toBe("free");
    expect(nextCameraFollowState("free", "pause")).toBe("free");
  });
});

describe("user location startup", () => {
  it("auto-starts for an existing grant without prompting a first-time visitor", () => {
    expect(permissionAllowsAutoStart("granted")).toBe(true);
    expect(permissionAllowsAutoStart("prompt")).toBe(false);
    expect(permissionAllowsAutoStart("denied")).toBe(false);
    expect(permissionAllowsAutoStart(undefined)).toBe(false);
  });

  it("uses a remembered opt-in when Firefox does not expose its temporary grant", () => {
    expect(permissionAllowsAutoStart("prompt", true)).toBe(true);
    expect(permissionAllowsAutoStart(undefined, true)).toBe(true);
    expect(permissionAllowsAutoStart("denied", true)).toBe(false);
  });

  it("treats the configured collection bounds as inclusive", () => {
    const bounds: [-74.3, 40.4, -73.6, 40.95] = [-74.3, 40.4, -73.6, 40.95];
    expect(locationIsWithinBounds(-73.98, 40.7, bounds)).toBe(true);
    expect(locationIsWithinBounds(-74.3, 40.4, bounds)).toBe(true);
    expect(locationIsWithinBounds(-74.31, 40.7, bounds)).toBe(false);
  });

  it("does not let an automatic first fix override user navigation", () => {
    expect(shouldCenterFirstFix("automatic", false, true)).toBe(true);
    expect(shouldCenterFirstFix("automatic", true, true)).toBe(false);
    expect(shouldCenterFirstFix("automatic", false, false)).toBe(false);
    expect(shouldCenterFirstFix("user", true, false)).toBe(true);
  });
});
