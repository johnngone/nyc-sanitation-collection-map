import type { ControlPosition, IControl, Map as MapLibreMap, MapOptions } from "maplibre-gl";

import { EXTRUDED_BUILDING_LAYER_ID, FLAT_BUILDING_LAYER_ID } from "./basemap";

export const CAMERA_TOLERANCE_DEGREES = 1;
export const EXTRUSION_DETAIL_ZOOM = 14;
export const MAP_INTERACTION_OPTIONS = {
  maxZoom: 18,
  maxPitch: 45,
  dragRotate: true,
  touchZoomRotate: true,
  touchPitch: true,
  pitchWithRotate: true,
  rollEnabled: false,
} satisfies Pick<MapOptions, "maxZoom" | "maxPitch" | "dragRotate" | "touchZoomRotate" | "touchPitch" | "pitchWithRotate" | "rollEnabled">;

export interface CameraPosition {
  bearing: number;
  pitch: number;
  zoom: number;
}

export interface MapViewState {
  extrusionLatched: boolean;
  showCompass: boolean;
  showExtrudedBuildings: boolean;
  showFlatBuildings: boolean;
}

function normalizedBearing(bearing: number): number {
  return ((bearing + 180) % 360 + 360) % 360 - 180;
}

export function mapViewState(camera: CameraPosition, extrusionLatched: boolean, allowLatch = true): MapViewState {
  const nextLatch = extrusionLatched
    || (allowLatch && camera.pitch > CAMERA_TOLERANCE_DEGREES && camera.zoom >= EXTRUSION_DETAIL_ZOOM);
  const cameraIsOriented = needsCameraReset(camera);
  return {
    extrusionLatched: nextLatch,
    showCompass: cameraIsOriented || nextLatch,
    showExtrudedBuildings: nextLatch,
    showFlatBuildings: !nextLatch || camera.zoom < EXTRUSION_DETAIL_ZOOM,
  };
}

export function needsCameraReset(camera: Pick<CameraPosition, "bearing" | "pitch">): boolean {
  return camera.pitch > CAMERA_TOLERANCE_DEGREES
    || Math.abs(normalizedBearing(camera.bearing)) > CAMERA_TOLERANCE_DEGREES;
}

export function compassTransform(bearing: number): string {
  return `rotate(${-normalizedBearing(bearing)}deg)`;
}

export function cameraTransitionDuration(prefersReducedMotion: boolean): number {
  return prefersReducedMotion ? 0 : 500;
}

function transitionDuration(): number {
  return cameraTransitionDuration(window.matchMedia("(prefers-reduced-motion: reduce)").matches);
}

function setLayerVisibility(map: MapLibreMap, layerId: string, visible: boolean): void {
  if (!map.getLayer(layerId)) return;
  const desired = visible ? "visible" : "none";
  const current = map.getLayoutProperty(layerId, "visibility") ?? "visible";
  if (current !== desired) map.setLayoutProperty(layerId, "visibility", desired);
}

function createCompassNeedle(): SVGSVGElement {
  const namespace = "http://www.w3.org/2000/svg";
  const needle = document.createElementNS(namespace, "svg");
  needle.setAttribute("class", "map-view-compass-needle");
  needle.setAttribute("viewBox", "0 0 29 29");
  needle.setAttribute("aria-hidden", "true");

  const north = document.createElementNS(namespace, "path");
  north.setAttribute("class", "map-view-compass-north");
  north.setAttribute("d", "m10.5 14 4-8 4 8z");
  const south = document.createElementNS(namespace, "path");
  south.setAttribute("class", "map-view-compass-south");
  south.setAttribute("d", "m10.5 16 4 8 4-8z");
  needle.append(north, south);
  return needle;
}

export class MapViewControl implements IControl {
  private map?: MapLibreMap;
  private container?: HTMLDivElement;
  private compassButton?: HTMLButtonElement;
  private compassIcon?: HTMLSpanElement;
  private extrusionLatched = false;
  private resettingView = false;

  getDefaultPosition(): ControlPosition {
    return "top-right";
  }

  onAdd(map: MapLibreMap): HTMLElement {
    this.map = map;
    this.container = document.createElement("div");
    this.container.className = "maplibregl-ctrl maplibregl-ctrl-group map-view-control";

    this.compassButton = document.createElement("button");
    this.compassButton.type = "button";
    this.compassButton.className = "maplibregl-ctrl-compass map-view-compass";
    this.compassButton.title = "Return to north and flat map";
    this.compassButton.setAttribute("aria-label", "Return to north and flat map");
    this.compassIcon = document.createElement("span");
    this.compassIcon.className = "maplibregl-ctrl-icon";
    this.compassIcon.setAttribute("aria-hidden", "true");
    this.compassIcon.append(createCompassNeedle());
    this.compassButton.append(this.compassIcon);
    this.compassButton.addEventListener("click", this.resetView);
    this.container.append(this.compassButton);

    map.on("rotate", this.sync);
    map.on("pitch", this.sync);
    map.on("zoom", this.sync);
    this.sync();
    return this.container;
  }

  onRemove(): void {
    if (this.map) {
      this.map.off("rotate", this.sync);
      this.map.off("pitch", this.sync);
      this.map.off("zoom", this.sync);
      this.map.off("moveend", this.finishReset);
    }
    this.compassButton?.removeEventListener("click", this.resetView);
    this.container?.remove();
    this.map = undefined;
  }

  sync = (): void => {
    if (!this.map || !this.container || !this.compassButton || !this.compassIcon) return;
    const state = mapViewState({
      bearing: this.map.getBearing(),
      pitch: this.map.getPitch(),
      zoom: this.map.getZoom(),
    }, this.extrusionLatched, !this.resettingView);
    this.extrusionLatched = state.extrusionLatched;
    this.container.hidden = !state.showCompass;
    this.compassButton.hidden = !state.showCompass;
    this.compassButton.setAttribute("aria-hidden", String(!state.showCompass));
    this.compassIcon.style.transform = compassTransform(this.map.getBearing());
    setLayerVisibility(this.map, FLAT_BUILDING_LAYER_ID, state.showFlatBuildings);
    setLayerVisibility(this.map, EXTRUDED_BUILDING_LAYER_ID, state.showExtrudedBuildings);
  };

  private resetView = (): void => {
    if (!this.map) return;
    const camera = { bearing: this.map.getBearing(), pitch: this.map.getPitch() };
    this.resettingView = true;
    this.extrusionLatched = false;
    this.sync();
    if (!needsCameraReset(camera)) {
      this.finishReset();
      return;
    }
    this.map.off("moveend", this.finishReset);
    this.map.once("moveend", this.finishReset);
    this.map.easeTo({ bearing: 0, pitch: 0, duration: transitionDuration() });
  };

  private finishReset = (): void => {
    this.resettingView = false;
    this.sync();
  };
}
