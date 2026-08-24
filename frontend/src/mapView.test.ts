import { describe, expect, it } from "vitest";

import { cameraViewState, nextCameraTransition } from "./mapView";

describe("camera view state", () => {
  it.each([
    [0, 0, false, false],
    [0.5, 0.5, false, false],
    [15, 0, false, true],
    [0, 45, true, true],
    [-30, 45, true, true],
    [360, 0, false, false],
  ])("maps bearing %s and pitch %s", (bearing, pitch, isThreeDimensional, showCompass) => {
    expect(cameraViewState(bearing, pitch)).toEqual({ isThreeDimensional, showCompass });
  });
});

describe("camera actions", () => {
  it("enters 3D without changing a rotated bearing and raises a low zoom", () => {
    expect(nextCameraTransition("toggle", { bearing: 28, pitch: 0, zoom: 12 })).toEqual({ pitch: 45, zoom: 14 });
  });

  it("enters 3D without zooming out", () => {
    expect(nextCameraTransition("toggle", { bearing: 0, pitch: 0, zoom: 16 })).toEqual({ pitch: 45, zoom: 16 });
  });

  it("returns to 2D while preserving zoom and bearing", () => {
    expect(nextCameraTransition("toggle", { bearing: -22, pitch: 45, zoom: 15 })).toEqual({ pitch: 0 });
  });

  it("resolves an interrupted transition from the actual camera pitch", () => {
    expect(nextCameraTransition("toggle", { bearing: 12, pitch: 18, zoom: 14 })).toEqual({ pitch: 0 });
    expect(cameraViewState(12, 0)).toEqual({ isThreeDimensional: false, showCompass: true });
  });

  it("resets both bearing and pitch without changing zoom", () => {
    expect(nextCameraTransition("reset", { bearing: 35, pitch: 45, zoom: 17 })).toEqual({ bearing: 0, pitch: 0 });
  });
});
