import { describe, expect, it } from "vitest";

import { cameraTransitionDuration, compassTransform, MAP_INTERACTION_OPTIONS, mapViewState, needsCameraReset } from "./mapView";

describe("map interactions", () => {
  it("enables desktop and touch rotation and pitch without camera roll", () => {
    expect(MAP_INTERACTION_OPTIONS).toEqual({
      maxZoom: 18,
      maxPitch: 50,
      dragRotate: true,
      touchZoomRotate: true,
      touchPitch: true,
      pitchWithRotate: true,
      rollEnabled: false,
    });
  });
});

describe("map view state", () => {
  it("starts flat, north-up, and unlatched", () => {
    expect(mapViewState({ bearing: 0, pitch: 0, zoom: 13 }, false)).toEqual({
      extrusionLatched: false,
      showCompass: false,
      showExtrudedBuildings: false,
      showFlatBuildings: true,
    });
  });

  it("shows the compass for rotation without latching extrusions", () => {
    expect(mapViewState({ bearing: 25, pitch: 0, zoom: 15 }, false)).toEqual({
      extrusionLatched: false,
      showCompass: true,
      showExtrudedBuildings: false,
      showFlatBuildings: true,
    });
  });

  it("allows low-zoom tilt without extruding buildings", () => {
    expect(mapViewState({ bearing: 0, pitch: 35, zoom: 13.9 }, false)).toEqual({
      extrusionLatched: false,
      showCompass: true,
      showExtrudedBuildings: false,
      showFlatBuildings: true,
    });
  });

  it("latches extrusions when a tilted camera reaches zoom 14", () => {
    expect(mapViewState({ bearing: -20, pitch: 2, zoom: 14 }, false)).toEqual({
      extrusionLatched: true,
      showCompass: true,
      showExtrudedBuildings: true,
      showFlatBuildings: false,
    });
  });

  it("retains extrusions after the user manually flattens the map", () => {
    expect(mapViewState({ bearing: 0, pitch: 0, zoom: 15 }, true)).toEqual({
      extrusionLatched: true,
      showCompass: true,
      showExtrudedBuildings: true,
      showFlatBuildings: false,
    });
  });

  it("shows flat footprints below detail zoom without clearing the latch", () => {
    const zoomedOut = mapViewState({ bearing: 0, pitch: 0, zoom: 13.5 }, true);
    expect(zoomedOut).toEqual({
      extrusionLatched: true,
      showCompass: true,
      showExtrudedBuildings: true,
      showFlatBuildings: true,
    });
    expect(mapViewState({ bearing: 0, pitch: 0, zoom: 14 }, zoomedOut.extrusionLatched).showFlatBuildings).toBe(false);
  });

  it("uses a one-degree camera tolerance", () => {
    expect(mapViewState({ bearing: 1, pitch: 1, zoom: 14 }, false).showCompass).toBe(false);
    expect(mapViewState({ bearing: 1.01, pitch: 0, zoom: 14 }, false).showCompass).toBe(true);
    expect(mapViewState({ bearing: 0, pitch: 1.01, zoom: 14 }, false).extrusionLatched).toBe(true);
  });

  it("does not relatch while the compass reset is in progress", () => {
    expect(mapViewState({ bearing: 20, pitch: 30, zoom: 15 }, false, false).extrusionLatched).toBe(false);
  });

  it("distinguishes a camera reset from clearing a north-up extrusion latch", () => {
    expect(needsCameraReset({ bearing: 0, pitch: 0 })).toBe(false);
    expect(needsCameraReset({ bearing: -15, pitch: 0 })).toBe(true);
    expect(needsCameraReset({ bearing: 0, pitch: 10 })).toBe(true);
  });
});

describe("compass presentation", () => {
  it.each([
    [0, "rotate(0deg)"],
    [30, "rotate(-30deg)"],
    [-45, "rotate(45deg)"],
    [360, "rotate(0deg)"],
  ])("counter-rotates bearing %s", (bearing, transform) => {
    expect(compassTransform(bearing)).toBe(transform);
  });

  it("honors reduced-motion preferences", () => {
    expect(cameraTransitionDuration(false)).toBe(500);
    expect(cameraTransitionDuration(true)).toBe(0);
  });
});
